"""LanceDB embedded vector backend (implements the Elasticsearch client method surface, so the repositories reuse it unchanged).

Design (mirroring how pgvector_store plugs in, with zero service dependency):
- every ES "index" maps to one LanceDB table (a local directory, `lancedb.connect_async`).
- the table shape comes from ``index_schemas.INDEX_SCHEMAS`` plus inference from the first batch: a vector column's dimension is
  the length of the first vector value (matching pgvector); datetime is always stored as an ISO-8601 string.
- writes: ``merge_insert("id")`` for an idempotent upsert; the id comes from _id/id/chunk_id/entity_id/event_id.
- kNN: cosine distance, ``_score = 1 - distance``; filter_query (ES bool/term/terms/range/exists)
  is translated into a DataFusion WHERE string.
- lexical: **real BM25** (tantivy FTS), handling ``multi_match`` correctly (including the ``title^3`` weight, merged by id
  taking the highest score) and the nested ``{"match": {"f": {"query": ...}}}`` - the two known defects of the pgvector
  degraded path, which this backend does not repeat; hence it declares LEXICAL_SEARCH and the multi_es strategy works locally.

Only the subset of methods the repositories actually call is exposed, with signatures matching ``ElasticsearchClient``.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime
from typing import Any

# Imported at the top (rather than lazily): the first cold load of the lancedb + pyarrow native libraries can take seconds, so it happens
# at module import time and cannot fall inside the timeout window of the start() health-check ping (a cold install once misread that as a failed connection).
import lancedb

from alicecore.core.storage.index_schemas import INDEX_SCHEMAS
from alicecore.utils import get_logger

logger = get_logger("storage.lancedb")

_IDENT = re.compile(r"[^a-zA-Z0-9_]")

# For indexes/fields not declared in INDEX_SCHEMAS, the vector column heuristic (matching pgvector)
_MIN_VECTOR_LEN = 8


def _ident(name: str) -> str:
    """Normalise an index or field name into a safe identifier."""
    return _IDENT.sub("_", name)


def _is_vector(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) > _MIN_VECTOR_LEN
        and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in value)
    )


def _sql_str(value: Any) -> str:
    """A SQL string literal (single quotes escaped)."""
    return "'" + str(value).replace("'", "''") + "'"


def _sql_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (datetime, date)):
        return _sql_str(value.isoformat())
    return _sql_str(value)


def _doc_id(doc: dict[str, Any], explicit: str | None = None) -> str | None:
    did = (
        explicit
        or doc.get("_id")
        or doc.get("id")
        or doc.get("chunk_id")
        or doc.get("entity_id")
        or doc.get("event_id")
    )
    return str(did) if did is not None else None


class LanceDBStore:
    """The LanceDB backend, with a method surface matching ElasticsearchClient."""

    def __init__(self, uri: str) -> None:
        self._uri = uri
        self._db: Any = None
        self._tables: dict[str, Any] = {}  # table name -> AsyncTable
        self._fts_ready: dict[str, set[str]] = {}  # table name -> the columns whose FTS index exists
        self._lock = asyncio.Lock()  # guards the table/index creation race

    @property
    def client(self) -> LanceDBStore:
        """Some repositories read the native client through ``es_client.client``; this returns self (method-surface compatible)."""
        return self

    # -- Connection / table management -----------------------------
    async def _conn(self) -> Any:
        if self._db is None:
            self._db = await lancedb.connect_async(self._uri)
        return self._db

    async def _table_names(self) -> list[str]:
        db = await self._conn()
        if hasattr(db, "list_tables"):
            result = await db.list_tables()
            # A newer version returns ListTablesResponse (.tables: list[str]); an older one returns a list
            names = getattr(result, "tables", result)
        else:
            names = await db.table_names()
        return [getattr(t, "name", t) for t in names]

    async def _open_table(self, index: str) -> Any | None:
        """Open an existing table; return None when it does not exist (matching the ES "missing index -> empty result" semantics)."""
        table = _ident(index)
        if table in self._tables:
            return self._tables[table]
        async with self._lock:
            if table in self._tables:
                return self._tables[table]
            if table not in await self._table_names():
                return None
            db = await self._conn()
            tbl = await db.open_table(table)
            self._tables[table] = tbl
            return tbl

    async def _ensure_table(self, index: str, batch: list[dict[str, Any]]) -> Any:
        """Open the table, or create it from the first batch of documents."""
        tbl = await self._open_table(index)
        if tbl is not None:
            return tbl
        table = _ident(index)
        async with self._lock:
            if table in self._tables:
                return self._tables[table]
            if table in await self._table_names():  # race: someone else created it
                db = await self._conn()
                tbl = await db.open_table(table)
            else:
                schema = self._build_schema(index, batch)
                db = await self._conn()
                tbl = await db.create_table(table, schema=schema)
            self._tables[table] = tbl
            return tbl

    def _build_schema(self, index: str, batch: list[dict[str, Any]]) -> Any:
        """Build the pyarrow schema from the declarative IndexSchema plus the first batch of documents."""
        import pyarrow as pa

        decl = INDEX_SCHEMAS.get(index)
        fields: list[Any] = [pa.field("id", pa.string())]
        seen: set[str] = {"id"}

        def _vector_dim(name: str) -> int:
            for doc in batch:
                v = doc.get(name)
                if _is_vector(v):
                    return len(v)
            # This vector column is missing from the first batch: borrow the dimension of another vector column in the same batch (one embedding model)
            for doc in batch:
                for v in doc.values():
                    if _is_vector(v):
                        return len(v)
            from alicecore.core.config.settings import get_settings

            return get_settings().embedding_dimensions or 1024

        if decl is not None:
            for f in decl.vector_fields:
                fields.append(pa.field(f, pa.list_(pa.float32(), _vector_dim(f))))
            for f in decl.text_fields + decl.keyword_fields + decl.datetime_fields:
                fields.append(pa.field(f, pa.string()))
            for f in decl.array_fields:
                fields.append(pa.field(f, pa.list_(pa.string())))
            for f in decl.bool_fields:
                fields.append(pa.field(f, pa.bool_()))
            for f in decl.int_fields:
                fields.append(pa.field(f, pa.int64()))
            seen.update(decl.all_fields())

        # Fields outside the declaration: inferred from the first batch's values (an unknown index takes this path entirely)
        for doc in batch:
            for name, value in doc.items():
                col = _ident(name)
                if col in seen or name == "_id" or value is None:
                    continue
                seen.add(col)
                fields.append(pa.field(col, self._infer_type(value)))
        return pa.schema(fields)

    @staticmethod
    def _infer_type(value: Any) -> Any:
        import pyarrow as pa

        if _is_vector(value):
            return pa.list_(pa.float32(), len(value))
        if isinstance(value, bool):
            return pa.bool_()
        if isinstance(value, int):
            return pa.int64()
        if isinstance(value, float):
            return pa.float64()
        if isinstance(value, (list, tuple)):
            return pa.list_(pa.string())
        return pa.string()  # str/datetime/dict and the rest become strings (dict -> JSON)

    def _normalize_row(self, tbl: Any, did: str, doc: dict[str, Any]) -> dict[str, Any]:
        """Normalise one row to the table schema: fill missing with None, datetime -> ISO, coerce types; extra fields are dropped."""
        import pyarrow as pa

        schema = tbl.schema if not asyncio.iscoroutine(tbl.schema) else None
        # AsyncTable.schema() is a coroutine; cache it on the instance so it is not awaited per row
        row: dict[str, Any] = {"id": did}
        for f in self._schema_fields(tbl):
            name, ftype = f
            if name == "id":
                continue
            value = doc.get(name)
            if value is None:
                row[name] = None
            elif pa.types.is_fixed_size_list(ftype):
                row[name] = [float(x) for x in value] if _is_vector(value) else None
            elif pa.types.is_list(ftype):
                row[name] = [str(x) for x in value] if isinstance(value, (list, tuple)) else None
            elif pa.types.is_boolean(ftype):
                row[name] = bool(value)
            elif pa.types.is_integer(ftype):
                row[name] = int(value)
            elif pa.types.is_floating(ftype):
                row[name] = float(value)
            else:  # string
                if isinstance(value, (datetime, date)):
                    row[name] = value.isoformat()
                elif isinstance(value, (dict, list, tuple)):
                    row[name] = json.dumps(value, ensure_ascii=False)
                else:
                    row[name] = str(value)
        _ = schema
        return row

    def _schema_fields(self, tbl: Any) -> list[tuple[str, Any]]:
        cached = getattr(tbl, "_alicecore_schema_fields", None)
        if cached is not None:
            return cached
        raise RuntimeError("The schema is not cached: an internal error, _cache_schema should have run first")

    async def _cache_schema(self, tbl: Any) -> None:
        if getattr(tbl, "_alicecore_schema_fields", None) is None:
            schema = await tbl.schema()
            tbl._alicecore_schema_fields = [(f.name, f.type) for f in schema]

    def _vector_columns(self, tbl: Any) -> set[str]:
        import pyarrow as pa

        return {n for n, t in self._schema_fields(tbl) if pa.types.is_fixed_size_list(t)}

    def _array_columns(self, tbl: Any) -> set[str]:
        import pyarrow as pa

        return {
            n
            for n, t in self._schema_fields(tbl)
            if pa.types.is_list(t) and not pa.types.is_fixed_size_list(t)
        }

    # -- Writes ----------------------------------------------------
    async def _write(self, index: str, rows: list[tuple[str, dict[str, Any]]]) -> int:
        if not rows:
            return 0
        tbl = await self._ensure_table(index, [doc for _, doc in rows])
        await self._cache_schema(tbl)
        normalized = [self._normalize_row(tbl, did, doc) for did, doc in rows]
        await (
            tbl.merge_insert("id")
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute(normalized)
        )
        return len(normalized)

    async def index_document(
        self,
        index: str,
        document: dict[str, Any],
        doc_id: str | None = None,
        routing: str | None = None,
    ) -> str:
        did = _doc_id(document, doc_id) or ""
        await self._write(index, [(did, document)])
        return did

    async def bulk_index(
        self,
        index: str,
        documents: list[dict[str, Any]],
        return_details: bool = False,
        routing: str | None = None,
    ) -> Any:
        rows = []
        for d in documents:
            did = _doc_id(d)
            if did is None:
                continue
            rows.append((did, {k: v for k, v in d.items() if k != "_id"}))
        n = await self._write(index, rows)
        if return_details:
            return {
                "success": True,
                "total": len(documents),
                "success_count": n,
                "error_count": 0,
                "errors": [],
            }
        return n

    # -- Methods shaped like the native ES client (BaseRepository calls them directly) --
    async def index(
        self,
        index: str,
        id: str | None = None,  # noqa: A002 - matching the native ES signature
        document: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        routing: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        doc = document if document is not None else (body or {})
        did = _doc_id(doc, id) or ""
        await self._write(index, [(did, doc)])
        return {"_id": did, "result": "created"}

    async def get(self, index: str, id: str, **kwargs: Any) -> dict[str, Any]:  # noqa: A002
        doc = await self.get_document(index, id)
        if doc is None:
            raise KeyError(f"document '{id}' not found in '{index}'")
        return {"_id": id, "_source": doc}

    async def delete(self, index: str, id: str, **kwargs: Any) -> dict[str, Any]:  # noqa: A002
        ok = await self.delete_document(index, id)
        if not ok:
            raise KeyError(f"document '{id}' not found in '{index}'")
        return {"result": "deleted"}

    async def bulk(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        # Compatible with the native bulk call surface; the real batching goes through bulk_index.
        return {"errors": False, "items": []}

    # -- filter_query (ES DSL) -> a WHERE string --------------------
    def _translate_filter(self, tbl: Any, fq: dict[str, Any] | None) -> str:
        if not fq:
            return ""
        array_cols = self._array_columns(tbl)

        def _col(f: str) -> str:
            if f == "_id":
                return "id"
            return _ident(f.removesuffix(".keyword"))

        def _clause(node: Any) -> str:
            if not isinstance(node, dict):
                return ""
            if "bool" in node:
                b = node["bool"]
                parts: list[str] = []
                for key in ("must", "filter"):
                    sub = b.get(key, [])
                    sub = sub if isinstance(sub, list) else [sub]
                    parts.extend(p for c in sub if (p := _clause(c)))
                should = b.get("should", [])
                should = should if isinstance(should, list) else [should]
                should_parts = [p for c in should if (p := _clause(c))]
                if should_parts:
                    # ES semantics: with must/filter present, should only contributes to scoring unless minimum_should_match>=1
                    msm = b.get("minimum_should_match", 0 if parts else 1)
                    if int(msm or 0) >= 1:
                        parts.append(" OR ".join(f"({p})" for p in should_parts))
                must_not = b.get("must_not", [])
                must_not = must_not if isinstance(must_not, list) else [must_not]
                parts.extend(f"NOT ({p})" for c in must_not if (p := _clause(c)))
                return " AND ".join(f"({p})" for p in parts) if parts else ""
            if "term" in node:
                body = node["term"]
                ((f, v),) = body.items()
                if isinstance(v, dict):  # the {"f": {"value": x}} form
                    v = v.get("value")
                col = _col(f)
                if v is False:
                    return f"({col} = false OR {col} IS NULL)"
                return f"{col} = {_sql_literal(v)}"
            if "terms" in node:
                ((f, vals),) = node["terms"].items()
                col = _col(f)
                values = list(vals)
                if not values:
                    return "1 = 0"
                literals = ", ".join(_sql_literal(v) for v in values)
                if col in array_cols:
                    return f"array_has_any({col}, [{literals}])"
                return f"{col} IN ({literals})"
            if "range" in node:
                ((f, conds),) = node["range"].items()
                col = _col(f)
                ops = {"gte": ">=", "gt": ">", "lte": "<=", "lt": "<"}
                parts = [
                    f"{col} {ops[op]} {_sql_literal(v)}" for op, v in conds.items() if op in ops
                ]
                return " AND ".join(parts)
            if "exists" in node:
                return f"{_col(node['exists']['field'])} IS NOT NULL"
            if "match_all" in node:
                return ""
            return ""  # scoring clauses such as match/multi_match are handled by the lexical path

        return _clause(fq)

    # ── kNN ─────────────────────────────────────────────────
    async def vector_search(
        self,
        index: str,
        field: str,
        vector: list[float],
        size: int = 10,
        filter_query: dict[str, Any] | None = None,
        routing: str | None = None,
        include_vector: bool = False,
    ) -> list[dict[str, Any]]:
        tbl = await self._open_table(index)
        if tbl is None:
            logger.warning(f"Vector search: index '{index}' does not exist, returning an empty result")
            return []
        await self._cache_schema(tbl)
        q = tbl.query().nearest_to([float(x) for x in vector])
        # With several vector columns (such as source_chunks' heading_vector/content_vector) the column must be named explicitly
        if _ident(field) in self._vector_columns(tbl):
            q = q.column(_ident(field))
        q = q.distance_type("cosine").limit(int(size))
        where = self._translate_filter(tbl, filter_query)
        if where:
            q = q.where(where)
        if not include_vector:
            keep = [n for n, _ in self._schema_fields(tbl) if n not in self._vector_columns(tbl)]
            q = q.select([*keep, "_distance"])  # name the computed column explicitly, so lance does not warn about auto projection
        rows = await q.to_list()
        return [
            {**{k: v for k, v in r.items() if k != "_distance"}, "_score": 1.0 - r["_distance"]}
            for r in rows
        ]

    # -- search (lexical BM25 + filtering) --------------------------
    async def search(
        self,
        index: str,
        query: dict[str, Any],
        size: int = 10,
        from_: int = 0,
        return_full_response: bool = False,
        routing: str | None = None,
        **kwargs: Any,
    ) -> Any:
        tbl = await self._open_table(index)
        if tbl is None:
            logger.warning(f"Search: index '{index}' does not exist, returning an empty result")
            return {"total": 0, "max_score": 0, "hits": []} if return_full_response else []
        await self._cache_schema(tbl)

        lexical, residual = self._split_lexical(query)
        where = self._translate_filter(tbl, residual)
        fetch = int(size) + int(from_)

        if lexical:
            text_query, fields = lexical
            hits = await self._fts_search(tbl, index, text_query, fields, where, fetch)
        else:
            q = tbl.query()
            if where:
                q = q.where(where)
            keep = [n for n, _ in self._schema_fields(tbl) if n not in self._vector_columns(tbl)]
            rows = await q.select(keep).limit(fetch).to_list()
            hits = [(r, 1.0) for r in rows]

        hits = hits[int(from_) : int(from_) + int(size)]
        if return_full_response:
            return {
                "total": len(hits),
                "max_score": max((s for _, s in hits), default=0),
                "hits": [
                    {"id": r.get("id"), "score": s, "source": r, "index": index} for r, s in hits
                ],
            }
        return [r for r, _ in hits]

    @staticmethod
    def _split_lexical(
        query: dict[str, Any] | None,
    ) -> tuple[tuple[str, list[tuple[str, float]]] | None, dict[str, Any] | None]:
        """Split the query into (the lexical clause, the remaining filter).

        The lexical clause returns (the query text, [(field, weight)]) and supports:
        - {"multi_match": {"query": q, "fields": ["title^3", "content"]}}
        - {"match": {"f": "text"}} and the nested {"match": {"f": {"query": "text", ...}}}
        Either may appear at the top level or inside bool.must; every other clause is kept as a filter.
        """

        def _parse(node: dict[str, Any]) -> tuple[str, list[tuple[str, float]]] | None:
            if "multi_match" in node:
                mm = node["multi_match"]
                fields: list[tuple[str, float]] = []
                for f in mm.get("fields", []):
                    name, _, boost = f.partition("^")
                    fields.append((name, float(boost) if boost else 1.0))
                return str(mm.get("query", "")), fields
            if "match" in node:
                ((f, v),) = node["match"].items()
                text = v.get("query") if isinstance(v, dict) else v
                return str(text), [(f, 1.0)]
            return None

        if not query:
            return None, None
        found = _parse(query)
        if found:
            return found, None
        if "bool" in query:
            b = dict(query["bool"])
            must = b.get("must", [])
            must = must if isinstance(must, list) else [must]
            rest = []
            for c in must:
                if found is None and isinstance(c, dict):
                    parsed = _parse(c)
                    if parsed:
                        found = parsed
                        continue
                rest.append(c)
            if found:
                b["must"] = rest
                return found, {"bool": b}
        return None, query

    async def _fts_search(
        self,
        tbl: Any,
        index: str,
        text_query: str,
        fields: list[tuple[str, float]],
        where: str,
        limit: int,
    ) -> list[tuple[dict[str, Any], float]]:
        """Run BM25 per field, merge by id taking max(weight x score), and return in descending order."""
        table = _ident(index)
        decl = INDEX_SCHEMAS.get(index)
        text_cols = set(decl.text_fields) if decl else set()
        schema_cols = {n for n, _ in self._schema_fields(tbl)}
        keep = [n for n in schema_cols if n not in self._vector_columns(tbl)]

        merged: dict[str, tuple[dict[str, Any], float]] = {}
        for field_name, boost in fields:
            col = _ident(field_name.removesuffix(".keyword"))
            if col not in schema_cols or (text_cols and col not in text_cols):
                continue
            await self._ensure_fts(tbl, table, col)
            q = tbl.query().nearest_to_text(text_query, columns=[col])
            if where:
                q = q.where(where)
            try:
                rows = await q.select([*keep, "_score"]).limit(int(limit)).to_list()
            except Exception as e:  # an empty table, a missing index segment and so on -> no hit for this field
                logger.warning(f"FTS search failed (index={index}, col={col}): {e}")
                continue
            for r in rows:
                score = float(r.pop("_score", 0.0)) * boost
                rid = str(r.get("id"))
                if rid not in merged or score > merged[rid][1]:
                    merged[rid] = (r, score)
        return sorted(merged.values(), key=lambda t: t[1], reverse=True)

    async def _ensure_fts(self, tbl: Any, table: str, col: str) -> None:
        ready = self._fts_ready.setdefault(table, set())
        if col in ready:
            return
        async with self._lock:
            if col in ready:
                return
            from lancedb.index import FTS

            try:
                await tbl.create_index(col, config=FTS())
            except Exception as e:  # already exists and so on -> idempotent
                logger.debug(f"FTS index creation skipped ({table}.{col}): {e}")
            ready.add(col)

    # -- Single document / metadata ---------------------------------
    async def get_document(self, index: str, doc_id: str) -> dict[str, Any] | None:
        tbl = await self._open_table(index)
        if tbl is None:
            return None
        await self._cache_schema(tbl)
        keep = [n for n, _ in self._schema_fields(tbl) if n not in self._vector_columns(tbl)]
        rows = await tbl.query().where(f"id = {_sql_str(doc_id)}").select(keep).limit(1).to_list()
        return rows[0] if rows else None

    async def delete_document(self, index: str, doc_id: str) -> bool:
        tbl = await self._open_table(index)
        if tbl is None:
            return False
        await self._cache_schema(tbl)
        if await self.get_document(index, doc_id) is None:
            return False
        await tbl.delete(f"id = {_sql_str(doc_id)}")
        return True

    async def update_document(self, index: str, doc_id: str, update_data: dict[str, Any]) -> bool:
        tbl = await self._open_table(index)
        if tbl is None:
            return False
        await self._cache_schema(tbl)
        if await self.get_document(index, doc_id) is None:
            logger.warning(f"Document {doc_id} does not exist")
            return False
        fields = dict(self._schema_fields(tbl))
        updates = {
            _ident(k): (v.isoformat() if isinstance(v, (datetime, date)) else v)
            for k, v in update_data.items()
            if _ident(k) in fields and k not in ("_id", "id")
        }
        if updates:
            await tbl.update(updates, where=f"id = {_sql_str(doc_id)}")
        return True

    async def count_documents(self, index: str, query: dict[str, Any] | None = None) -> int:
        tbl = await self._open_table(index)
        if tbl is None:
            return 0
        await self._cache_schema(tbl)
        where = self._translate_filter(tbl, query)
        return int(await tbl.count_rows(where or None))

    async def create_index(self, *args: Any, **kwargs: Any) -> bool:
        return True  # tables are created on demand

    async def index_exists(self, index: str) -> bool:
        return _ident(index) in await self._table_names()

    async def delete_index(self, index: str) -> bool:
        table = _ident(index)
        if table in await self._table_names():
            db = await self._conn()
            await db.drop_table(table)
        self._tables.pop(table, None)
        self._fts_ready.pop(table, None)
        return True

    async def ping(self) -> bool:
        try:
            await self._table_names()
            return True
        except Exception as e:
            logger.error(f"The LanceDB connection check failed: {e}")
            return False

    async def check_connection(self) -> bool:
        return await self.ping()

    async def close(self) -> None:
        db, self._db = self._db, None
        self._tables.clear()
        self._fts_ready.clear()
        if db is not None and hasattr(db, "close"):
            result = db.close()
            if asyncio.iscoroutine(result):
                await result
