"""OceanBase vector backend (reuses the OB relational engine, so one instance serves both SQL and vectors).

Design (mirroring how pgvector_store plugs in, single-database deployment):
- **reuses the relational OceanBase engine** (MySQL protocol, aiomysql); it needs db_provider=oceanbase.
- every ES "index" maps to one table, whose columns are inferred from the written documents (a vector column -> ``VECTOR(N)`` + an HNSW cosine index;
  scalars -> longtext/bigint/double/tinyint; anything else -> json). The dimension comes from the first vector's length (matching pgvector).
- kNN: ``cosine_distance(col, '[...]')``, ``_score = 1 - distance``; with a vector index it uses ``ORDER BY ... APPROXIMATE``
  (ANN), otherwise exact brute force (still correct); filter_query (ES bool/term/terms/range/exists) -> a SQL WHERE.
- BM25/lexical degrades to ``LIKE`` (not real BM25), so LEXICAL_SEARCH is not declared and the capability check rejects the multi_es strategy.

Prerequisite: OceanBase **V4.3.3+**, and the tenant needs ``ob_vector_memory_limit_percentage>0`` before a vector index can be created or used;
without it, index creation fails -> it degrades to exact search automatically (with a warning) and still works.

Only the subset of methods the repositories actually call is exposed, with signatures matching ``ElasticsearchClient``.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any

from sqlalchemy import text

from alicecore.utils import get_logger

logger = get_logger("storage.oceanbase")

_IDENT = re.compile(r"[^a-zA-Z0-9_]")
_MIN_VECTOR_LEN = 8


def _ident(name: str) -> str:
    """Normalise an index or field name into a safe SQL identifier (only [a-zA-Z0-9_])."""
    return _IDENT.sub("_", name)


def _is_vector(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) > _MIN_VECTOR_LEN
        and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in value)
    )


def _vec_literal(vec: list[float]) -> str:
    """An OceanBase vector literal: the string ``'[f0,f1,...]'``."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _col_type(value: Any) -> str:
    if _is_vector(value):
        return f"VECTOR({len(value)})"
    if isinstance(value, bool):
        return "tinyint(1)"
    if isinstance(value, int):
        return "bigint"
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "longtext"
    return "json"


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


class OceanBaseVectorStore:
    """The OceanBase vector backend, with a method surface matching ElasticsearchClient."""

    def __init__(self) -> None:
        self._ready_tables: set[str] = set()
        self._approx_cols: set[str] = set()  # "table.col" whose vector index was created -> APPROXIMATE is usable

    @property
    def client(self) -> OceanBaseVectorStore:
        """Some repositories read the native client through ``es_client.client``; this returns self (method-surface compatible)."""
        return self

    # -- Underneath: the relational engine (OceanBase / aiomysql) ----
    def _engine(self) -> Any:
        from alicecore.db.base import get_engine

        return get_engine()

    # -- Pure SQL construction (unit-testable, no I/O) ---------------
    @staticmethod
    def _add_column_sql(table: str, col: str, value: Any) -> str:
        return f"ALTER TABLE `{table}` ADD COLUMN `{col}` {_col_type(value)}"

    @staticmethod
    def _vector_index_sql(table: str, col: str) -> str:
        return (
            f"CREATE VECTOR INDEX `{table}_{col}_vidx` ON `{table}`(`{col}`) "
            f"WITH (distance=cosine, type=hnsw)"
        )

    @staticmethod
    def _upsert_sql(table: str, cols: list[str], placeholders: list[str]) -> str:
        updates = ", ".join(f"`{c}` = VALUES(`{c}`)" for c in cols if c != "id")
        cols_sql = ", ".join(f"`{c}`" for c in cols)
        if updates:
            return (
                f"INSERT INTO `{table}` ({cols_sql}) VALUES ({', '.join(placeholders)}) "
                f"ON DUPLICATE KEY UPDATE {updates}"
            )
        return f"INSERT INTO `{table}` (`id`) VALUES (:id) ON DUPLICATE KEY UPDATE `id` = VALUES(`id`)"

    def _knn_sql(
        self,
        table: str,
        col: str,
        vector: list[float],
        select_cols: list[str],
        where: str,
        size: int,
    ) -> str:
        """The kNN SQL. The vector literal is inlined (OB's APPROXIMATE requires a comparison against a constant vector)."""
        qv = _vec_literal(vector)
        select_sql = ", ".join(f"`{c}`" for c in select_cols)
        where_sql = f"WHERE {where}" if where else ""
        approx = "APPROXIMATE " if f"{table}.{col}" in self._approx_cols else ""
        return (
            f"SELECT {select_sql}, (1 - cosine_distance(`{col}`, '{qv}')) AS _score "
            f"FROM `{table}` {where_sql} "
            f"ORDER BY cosine_distance(`{col}`, '{qv}') {approx}LIMIT {int(size)}"
        )

    def _translate_filter(self, fq: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
        params: dict[str, Any] = {}
        counter = [0]

        def _p(val: Any) -> str:
            counter[0] += 1
            key = f"f{counter[0]}"
            params[key] = val
            return key

        def _col(f: str) -> str:
            return "id" if f == "_id" else _ident(f.removesuffix(".keyword"))

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
                    msm = b.get("minimum_should_match", 0 if parts else 1)
                    if int(msm or 0) >= 1:
                        parts.append(" OR ".join(f"({p})" for p in should_parts))
                must_not = b.get("must_not", [])
                must_not = must_not if isinstance(must_not, list) else [must_not]
                parts.extend(f"NOT ({p})" for c in must_not if (p := _clause(c)))
                return " AND ".join(f"({p})" for p in parts) if parts else ""
            if "term" in node:
                ((f, v),) = node["term"].items()
                if isinstance(v, dict):
                    v = v.get("value")
                col = _col(f)
                if v is False:
                    return f"(`{col}` = 0 OR `{col}` IS NULL)"
                return f"`{col}` = :{_p(v)}"
            if "terms" in node:
                ((f, vals),) = node["terms"].items()
                col = _col(f)
                values = list(vals)
                if not values:
                    return "1 = 0"
                keys = ", ".join(f":{_p(v)}" for v in values)
                return f"`{col}` IN ({keys})"
            if "range" in node:
                ((f, conds),) = node["range"].items()
                col = _col(f)
                ops = {"gte": ">=", "gt": ">", "lte": "<=", "lt": "<"}
                return " AND ".join(
                    f"`{col}` {ops[op]} :{_p(v)}" for op, v in conds.items() if op in ops
                )
            if "exists" in node:
                return f"`{_col(node['exists']['field'])}` IS NOT NULL"
            return ""

        if not fq:
            return "", params
        return _clause(fq), params

    # -- Connectivity ------------------------------------------------
    async def ping(self) -> bool:
        try:
            async with self._engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as e:
            logger.error(f"The OceanBase connection check failed: {e}")
            return False

    async def check_connection(self) -> bool:
        return await self.ping()

    # -- Table / column creation (inferred from the documents) -------
    async def _ensure_table(self, index: str, document: dict[str, Any]) -> None:
        table = _ident(index)
        async with self._engine().begin() as conn:
            await conn.execute(
                text(f"CREATE TABLE IF NOT EXISTS `{table}` (`id` VARCHAR(255) PRIMARY KEY)")
            )
            existing = {
                r[0]
                for r in (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = :t AND table_schema = DATABASE()"
                        ),
                        {"t": table},
                    )
                ).fetchall()
            }
            for field, value in document.items():
                if field in ("_id", "id") or value is None:
                    continue
                col = _ident(field)
                if col in existing:
                    continue
                await conn.execute(text(self._add_column_sql(table, col, value)))
                if _is_vector(value):
                    await self._try_create_vector_index(conn, table, col)
        self._ready_tables.add(table)

    async def _try_create_vector_index(self, conn: Any, table: str, col: str) -> None:
        """Try to create the HNSW vector index; on failure (for example a tenant without ob_vector_memory_limit_percentage) degrade to exact search."""
        try:
            await conn.execute(text(self._vector_index_sql(table, col)))
            self._approx_cols.add(f"{table}.{col}")
        except Exception as e:
            logger.warning(
                f"Creating the OceanBase vector index failed (`{table}`.`{col}`), degrading to exact search; "
                f"for ANN, set ob_vector_memory_limit_percentage>0 on the tenant. Reason: {e}"
            )

    # -- Writes ------------------------------------------------------
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
        rows: list[tuple[str, dict[str, Any]]] = []
        for d in documents:
            did = _doc_id(d)
            if did is None:
                continue
            rows.append((did, {k: v for k, v in d.items() if k != "_id"}))
        await self._write(index, rows)
        n = len(rows)
        if return_details:
            return {
                "success": True,
                "total": len(documents),
                "success_count": n,
                "error_count": 0,
                "errors": [],
            }
        return n

    async def _write(self, index: str, rows: list[tuple[str, dict[str, Any]]]) -> None:
        if not rows:
            return
        await self._ensure_table(index, rows[0][1])
        table = _ident(index)
        async with self._engine().begin() as conn:
            for did, doc in rows:
                cols = ["id"]
                placeholders = [":id"]
                params: dict[str, Any] = {"id": did}
                for field, value in doc.items():
                    if field in ("_id", "id") or value is None:
                        continue
                    col = _ident(field)
                    cols.append(col)
                    if _is_vector(value):
                        placeholders.append(f":{col}")
                        params[col] = _vec_literal(value)  # a string -> OB converts it to VECTOR
                    elif isinstance(value, (list, dict)):
                        placeholders.append(f":{col}")
                        params[col] = json.dumps(value, ensure_ascii=False)
                    elif isinstance(value, (datetime, date)):
                        placeholders.append(f":{col}")
                        params[col] = value.isoformat()
                    else:
                        placeholders.append(f":{col}")
                        params[col] = value
                await conn.execute(text(self._upsert_sql(table, cols, placeholders)), params)

    # -- Methods shaped like the native ES client (some repositories call them directly) --
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
        return {"errors": False, "items": []}

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
        table = _ident(index)
        col = _ident(field)
        if table not in self._ready_tables and not await self._table_exists(table):
            return []
        where, params = self._translate_filter(filter_query)
        cols = await self._scalar_columns(table, include_vector)
        sql = self._knn_sql(table, col, vector, cols, where, size)
        async with self._engine().connect() as conn:
            result = await conn.execute(text(sql), params)
            return [dict(r._mapping) for r in result.fetchall()]

    # -- search (match -> LIKE degradation; term/terms filtering) ----
    async def search(
        self,
        index: str,
        query: dict[str, Any],
        size: int = 10,
        from_: int = 0,
        routing: str | None = None,
        return_full_response: bool = False,
        **kwargs: Any,
    ) -> Any:
        table = _ident(index)
        if not await self._table_exists(table):
            return {"total": 0, "max_score": 0, "hits": []} if return_full_response else []
        where, params = self._translate_filter(query if "bool" in (query or {}) else None)
        match = self._extract_match(query)
        if match:
            f, txt = match
            params["m"] = f"%{txt}%"
            match_where = f"`{_ident(f)}` LIKE :m"
            where = f"({where}) AND {match_where}" if where else match_where
        cols = await self._scalar_columns(table, include_vector=False)
        select_sql = ", ".join(f"`{c}`" for c in cols)
        where_sql = f"WHERE {where}" if where else ""
        sql = (
            f"SELECT {select_sql} FROM `{table}` {where_sql} "
            f"LIMIT {int(size)} OFFSET {int(from_)}"
        )
        async with self._engine().connect() as conn:
            rows = [dict(r._mapping) for r in (await conn.execute(text(sql), params)).fetchall()]
        if return_full_response:
            return {
                "total": len(rows),
                "max_score": 1.0 if rows else 0,
                "hits": [
                    {"id": r.get("id"), "score": 1.0, "source": r, "index": index} for r in rows
                ],
            }
        return rows

    @staticmethod
    def _extract_match(query: dict[str, Any] | None) -> tuple[str, str] | None:
        def _parse(node: dict[str, Any]) -> tuple[str, str] | None:
            if "multi_match" in node:
                mm = node["multi_match"]
                fields = mm.get("fields", [])
                first = fields[0].split("^")[0] if fields else ""
                return (first, str(mm.get("query", ""))) if first else None
            if "match" in node:
                ((f, v),) = node["match"].items()
                text_val = v.get("query") if isinstance(v, dict) else v
                return f, str(text_val)
            return None

        if not query:
            return None
        found = _parse(query)
        if found:
            return found
        if "bool" in query:
            for c in query["bool"].get("must", []):
                if isinstance(c, dict) and (parsed := _parse(c)):
                    return parsed
        return None

    # -- Metadata ----------------------------------------------------
    async def _table_exists(self, table: str) -> bool:
        async with self._engine().connect() as conn:
            r = await conn.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_name = :t AND table_schema = DATABASE()"
                ),
                {"t": table},
            )
            return bool(r.scalar())

    async def _scalar_columns(self, table: str, include_vector: bool) -> list[str]:
        async with self._engine().connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_name = :t AND table_schema = DATABASE()"
                    ),
                    {"t": table},
                )
            ).fetchall()
        return [c for c, dtype in rows if include_vector or str(dtype).lower() != "vector"]

    async def count_documents(self, index: str, query: dict[str, Any] | None = None) -> int:
        table = _ident(index)
        if not await self._table_exists(table):
            return 0
        where, params = self._translate_filter(query if query and "bool" in query else None)
        where_sql = f"WHERE {where}" if where else ""
        async with self._engine().connect() as conn:
            r = await conn.execute(text(f"SELECT count(*) FROM `{table}` {where_sql}"), params)
            return int(r.scalar() or 0)

    async def get_document(self, index: str, doc_id: str) -> dict[str, Any] | None:
        table = _ident(index)
        if not await self._table_exists(table):
            return None
        cols = await self._scalar_columns(table, include_vector=False)
        select_sql = ", ".join(f"`{c}`" for c in cols)
        async with self._engine().connect() as conn:
            r = await conn.execute(
                text(f"SELECT {select_sql} FROM `{table}` WHERE `id` = :id"), {"id": doc_id}
            )
            row = r.fetchone()
            return dict(row._mapping) if row else None

    async def delete_document(self, index: str, doc_id: str) -> bool:
        table = _ident(index)
        if not await self._table_exists(table):
            return False
        async with self._engine().begin() as conn:
            await conn.execute(text(f"DELETE FROM `{table}` WHERE `id` = :id"), {"id": doc_id})
        return True

    async def update_document(self, index: str, doc_id: str, update_data: dict[str, Any]) -> bool:
        if await self.get_document(index, doc_id) is None:
            return False
        await self._write(index, [(doc_id, {**update_data, "id": doc_id})])
        return True

    async def create_index(self, *args: Any, **kwargs: Any) -> bool:
        return True  # tables are created on demand

    async def index_exists(self, index: str) -> bool:
        return await self._table_exists(_ident(index))

    async def delete_index(self, index: str) -> bool:
        async with self._engine().begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS `{_ident(index)}`"))
        self._ready_tables.discard(_ident(index))
        return True

    async def close(self) -> None:
        return None
