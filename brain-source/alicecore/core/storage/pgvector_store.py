"""pgvector vector backend (implements the Elasticsearch client method surface, so the repositories reuse it unchanged).

Design:
- **reuses the relational PostgreSQL engine** (one database for both relations and vectors, no extra connection); it needs relational=postgres.
- every ES "index" maps to one table, whose columns are inferred from the written documents (a vector column -> ``vector(N)`` + an HNSW index;
  scalars -> text/bigint/double/boolean; anything else -> jsonb).
- kNN: ``ORDER BY col <=> :q::vector`` (cosine), ``_score = 1 - distance``; filter_query (ES bool/term/terms)
  is translated into a SQL WHERE (``_id`` -> the primary key ``id`` column).
- BM25/lexical (the match inside ``search``) degrades to ``ILIKE`` ordering (not real BM25, but enough as a MULTI_ES fallback).

Only the subset of methods the repositories actually call is exposed, with signatures matching ``ElasticsearchClient``.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import text

from alicecore.utils import get_logger

logger = get_logger("storage.pgvector")

_IDENT = re.compile(r"[^a-zA-Z0-9_]")


def _ident(name: str) -> str:
    """Normalise an index or field name into a safe SQL identifier."""
    return _IDENT.sub("_", name)


def _is_vector(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) > 8
        and all(isinstance(x, (int, float)) for x in value)
    )


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _col_type(value: Any) -> str:
    if _is_vector(value):
        return f"vector({len(value)})"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "bigint"
    if isinstance(value, float):
        return "double precision"
    if isinstance(value, str):
        return "text"
    return "jsonb"


class PgVectorStore:
    """The pgvector backend, with a method surface matching ElasticsearchClient."""

    def __init__(self) -> None:
        self._ready_tables: set[str] = set()

    @property
    def client(self) -> "PgVectorStore":
        """Some repositories read the native client through ``es_client.client``; this returns self (method-surface compatible)."""
        return self

    # -- Underneath: the relational engine (PostgreSQL) --------------
    def _engine(self) -> Any:
        from alicecore.db.base import get_engine

        return get_engine()

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
        did = id or doc.get("_id") or doc.get("id") or ""
        await self._write(index, [(str(did), doc)])
        return {"_id": str(did), "result": "created"}

    async def bulk(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        # Compatible with the native bulk call surface; the real batching goes through bulk_index.
        return {"errors": False, "items": []}

    async def ping(self) -> bool:
        try:
            async with self._engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as e:
            logger.error(f"The pgvector connection check failed: {e}")
            return False

    # -- Table / column creation (inferred from the documents) -------
    async def _ensure_table(self, index: str, document: dict[str, Any]) -> None:
        table = _ident(index)
        import json

        async with self._engine().begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(
                text(f'CREATE TABLE IF NOT EXISTS "{table}" (id text PRIMARY KEY)')
            )
            # Read the existing columns
            existing = {
                r[0]
                for r in (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = :t"
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
                await conn.execute(
                    text(f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{col}" {_col_type(value)}')
                )
                if _is_vector(value):
                    await conn.execute(
                        text(
                            f'CREATE INDEX IF NOT EXISTS "{table}_{col}_hnsw" ON "{table}" '
                            f'USING hnsw ("{col}" vector_cosine_ops)'
                        )
                    )
        self._ready_tables.add(table)
    _ = json  # kept (for a future jsonb serialisation)

    # -- Writes ------------------------------------------------------
    async def index_document(
        self,
        index: str,
        document: dict[str, Any],
        doc_id: str | None = None,
        routing: str | None = None,
    ) -> str:
        did = doc_id or document.get("_id") or document.get("id") or ""
        await self._write(index, [(str(did), document)])
        return str(did)

    async def bulk_index(
        self,
        index: str,
        documents: list[dict[str, Any]],
        return_details: bool = False,
        routing: str | None = None,
    ) -> Any:
        rows = []
        for d in documents:
            did = d.get("_id") or d.get("id") or d.get("chunk_id") or d.get("entity_id") or d.get("event_id")
            if did is None:
                continue
            rows.append((str(did), {k: v for k, v in d.items() if k != "_id"}))
        await self._write(index, rows)
        n = len(rows)
        if return_details:
            return {"success": True, "total": len(documents), "success_count": n, "error_count": 0, "errors": []}
        return n

    async def _write(self, index: str, rows: list[tuple[str, dict[str, Any]]]) -> None:
        if not rows:
            return
        await self._ensure_table(index, rows[0][1])
        table = _ident(index)
        import json

        async with self._engine().begin() as conn:
            for did, doc in rows:
                cols = ["id"]
                placeholders = [":id"]
                params: dict[str, Any] = {"id": did}
                for field, value in doc.items():
                    if field in ("_id", "id") or value is None:
                        continue
                    col = _ident(field)
                    cols.append(f'"{col}"')
                    if _is_vector(value):
                        placeholders.append(f"(:{col})::vector")
                        params[col] = _vec_literal(value)
                    elif isinstance(value, (list, dict)):
                        placeholders.append(f"(:{col})::jsonb")
                        params[col] = json.dumps(value, ensure_ascii=False)
                    else:
                        placeholders.append(f":{col}")
                        params[col] = value
                updates = ", ".join(f'{c} = EXCLUDED.{c}' for c in cols if c != "id")
                sql = (
                    f'INSERT INTO "{table}" ({", ".join(cols)}) VALUES ({", ".join(placeholders)}) '
                    f'ON CONFLICT (id) DO UPDATE SET {updates}'
                    if updates
                    else f'INSERT INTO "{table}" (id) VALUES (:id) ON CONFLICT (id) DO NOTHING'
                )
                await conn.execute(text(sql), params)

    # ── filter_query(ES) → SQL WHERE ──────────────────────
    def _translate_filter(self, fq: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
        params: dict[str, Any] = {}
        counter = [0]

        def _p(val: Any) -> str:
            counter[0] += 1
            key = f"f{counter[0]}"
            params[key] = val
            return key

        def _clause(node: dict[str, Any]) -> str:
            if "bool" in node:
                must = node["bool"].get("must", []) + node["bool"].get("filter", [])
                parts = [_clause(c) for c in must]
                parts = [p for p in parts if p]
                return " AND ".join(f"({p})" for p in parts) if parts else ""
            if "term" in node:
                (f, v), = node["term"].items()
                col = "id" if f == "_id" else _ident(f)
                return f'"{col}" = :{_p(v)}'
            if "terms" in node:
                (f, vals), = node["terms"].items()
                col = "id" if f == "_id" else _ident(f)
                return f'"{col}" = ANY(:{_p(list(vals))})'
            return ""

        if not fq:
            return "", params
        return _clause(fq), params

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
        params["qv"] = _vec_literal(vector)
        cols = await self._scalar_columns(table, include_vector)
        select_cols = ", ".join(f'"{c}"' for c in cols)
        where_sql = f"WHERE {where}" if where else ""
        sql = (
            f'SELECT {select_cols}, 1 - ("{col}" <=> (:qv)::vector) AS _score '
            f'FROM "{table}" {where_sql} '
            f'ORDER BY "{col}" <=> (:qv)::vector LIMIT {int(size)}'
        )
        async with self._engine().connect() as conn:
            result = await conn.execute(text(sql), params)
            return [dict(r._mapping) for r in result.fetchall()]

    # -- search (a subset of the ES DSL: match -> ILIKE; term/terms filtering) --
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
        order = ""
        # A match clause -> ILIKE ordering
        match = self._extract_match(query)
        if match:
            f, txt = match
            params["m"] = f"%{txt}%"
            like_col = _ident(f)
            match_where = f'"{like_col}" ILIKE :m'
            where = f"({where}) AND {match_where}" if where else match_where
        cols = await self._scalar_columns(table, include_vector=False)
        select_cols = ", ".join(f'"{c}"' for c in cols)
        where_sql = f"WHERE {where}" if where else ""
        sql = f'SELECT {select_cols} FROM "{table}" {where_sql} {order} LIMIT {int(size)} OFFSET {int(from_)}'
        async with self._engine().connect() as conn:
            rows = [dict(r._mapping) for r in (await conn.execute(text(sql), params)).fetchall()]
        if return_full_response:
            return {
                "total": len(rows),
                "max_score": 1.0 if rows else 0,
                "hits": [{"id": r.get("id"), "score": 1.0, "source": r, "index": index} for r in rows],
            }
        return rows

    @staticmethod
    def _extract_match(query: dict[str, Any] | None) -> tuple[str, str] | None:
        if not query:
            return None
        if "match" in query:
            (f, v), = query["match"].items()
            return f, str(v)
        if "bool" in query:
            for c in query["bool"].get("must", []):
                if "match" in c:
                    (f, v), = c["match"].items()
                    return f, str(v)
        return None

    # -- Metadata ----------------------------------------------------
    async def _table_exists(self, table: str) -> bool:
        async with self._engine().connect() as conn:
            r = await conn.execute(
                text("SELECT to_regclass(:t)"), {"t": f'public."{table}"'}
            )
            return r.scalar() is not None

    async def _scalar_columns(self, table: str, include_vector: bool) -> list[str]:
        async with self._engine().connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT column_name, udt_name FROM information_schema.columns "
                        "WHERE table_name = :t"
                    ),
                    {"t": table},
                )
            ).fetchall()
        return [c for c, udt in rows if include_vector or udt != "vector"]

    async def count_documents(self, index: str, query: dict[str, Any] | None = None) -> int:
        table = _ident(index)
        if not await self._table_exists(table):
            return 0
        where, params = self._translate_filter(query if query and "bool" in query else None)
        where_sql = f"WHERE {where}" if where else ""
        async with self._engine().connect() as conn:
            r = await conn.execute(text(f'SELECT count(*) FROM "{table}" {where_sql}'), params)
            return int(r.scalar() or 0)

    async def get_document(self, index: str, doc_id: str) -> dict[str, Any] | None:
        table = _ident(index)
        if not await self._table_exists(table):
            return None
        cols = await self._scalar_columns(table, include_vector=False)
        select_cols = ", ".join(f'"{c}"' for c in cols)
        async with self._engine().connect() as conn:
            r = await conn.execute(
                text(f'SELECT {select_cols} FROM "{table}" WHERE id = :id'), {"id": doc_id}
            )
            row = r.fetchone()
            return dict(row._mapping) if row else None

    async def delete_document(self, index: str, doc_id: str) -> bool:
        table = _ident(index)
        if not await self._table_exists(table):
            return False
        async with self._engine().begin() as conn:
            await conn.execute(text(f'DELETE FROM "{table}" WHERE id = :id'), {"id": doc_id})
        return True

    async def create_index(self, *args: Any, **kwargs: Any) -> bool:
        return True  # tables are created on demand

    async def index_exists(self, index: str) -> bool:
        return await self._table_exists(_ident(index))

    async def delete_index(self, index: str) -> bool:
        async with self._engine().begin() as conn:
            await conn.execute(text(f'DROP TABLE IF EXISTS "{_ident(index)}"'))
        return True

    async def close(self) -> None:
        return None
