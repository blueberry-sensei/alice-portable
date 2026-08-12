"""Expose the SAG knowledge base's retrieval, entity and raw-text capabilities as a standard MCP server.

A SAG instance builds exactly one FastMCP server. A call may span every source, or be narrowed to a single
source through ``source_id``: the HTTP wrapper, the in-process agent and the stdio entry point all inject
the currently visible sources through ``MCPScope``, so the tools themselves do not depend on the transport.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, TypedDict

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from sag_api.core.telemetry import AgentEventRecord, emit_agent_event
from sag_api.services import sub_agent_execution
from sag_api.services.retrieval_service import retrieve_relevant_sections

if TYPE_CHECKING:
    from sag_api.db.models import Document, Source
    from sag_api.sag import EngineManager


class MCPToolDetail(TypedDict):
    name: str
    label: str
    description: str


MCP_TOOL_DETAILS: tuple[MCPToolDetail, ...] = (
    {
        "name": "list_sources",
        "label": "List sources",
        "description": "Show the knowledge sources you can currently reach with their document and chunk counts, and obtain source_id.",
    },
    {
        "name": "search",
        "label": "Semantic search",
        "description": "Find related material by meaning; best for natural-language questions, concepts and vague phrasing. Returns evidence snippets and chunk_id.",
    },
    {
        "name": "get_entity",
        "label": "Look up entity",
        "description": "Find a person, organisation or concept and summarise the context around it in the material.",
    },
    {
        "name": "list_documents",
        "label": "List documents",
        "description": "List documents with their processing status and chunk counts, and obtain document_id.",
    },
    {
        "name": "outline",
        "label": "Document outline",
        "description": "View the section and chunk structure of one document and obtain chunk_id, to locate content quickly.",
    },
    {
        "name": "grep",
        "label": "Exact search",
        "description": "Search the raw text literally; best for proper nouns, identifiers, fixed phrases and code. Returns matching context and chunk_id.",
    },
    {
        "name": "read",
        "label": "Read raw text by line",
        "description": "Read a document's raw text page by page in line ranges, for following continuous context.",
    },
    {
        "name": "get_chunk",
        "label": "Read chunk",
        "description": "Read one chunk's full raw text by chunk_id, to verify and cite evidence.",
    },
    {
        "name": "list_sub_agents",
        "label": "List configured sub-agents",
        "description": (
            "Read the enabled Settings → Sub Agents registry from Brain. "
            "Use this exact source of truth instead of guessing REST endpoints or inspecting host CLIs. "
            "Credentials are never returned."
        ),
    },
    {
        "name": "ask_sub_agent",
        "label": "Ask a configured sub-agent",
        "description": (
            "Send a bounded analysis task to one enabled provider/model using the credential stored inside Brain. "
            "The sub-agent cannot read or edit the project filesystem, so include the relevant code/context. "
            "Brain records the call and result preview in Telemetry."
        ),
    },
    {
        "name": "log_agent_task",
        "label": "Log a delegated task",
        "description": (
            "Record a task run outside Brain, such as a host CLI sub-agent. "
            "Do not use this after ask_sub_agent because that tool records its own verified telemetry."
        ),
    },
)
MCP_TOOL_NAMES = tuple(tool["name"] for tool in MCP_TOOL_DETAILS)
MCP_TOOL_LABELS = {tool["name"]: tool["label"] for tool in MCP_TOOL_DETAILS}
MCP_TOOL_DESCRIPTIONS = {tool["name"]: tool["description"] for tool in MCP_TOOL_DETAILS}
READ_ONLY_TOOL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

SourceId = Annotated[
    str,
    Field(description="Optional. Comes from list_sources; leave it empty to query every visible source."),
]
DocumentId = Annotated[str, Field(description="Document ID from list_documents.")]
ChunkId = Annotated[
    str,
    Field(description="Chunk ID from a search, outline or grep result."),
]


@dataclass(frozen=True)
class MCPScope:
    """The sources visible to one MCP call, together with their warm engines."""

    engine_manager: EngineManager
    sources: tuple[Source, ...]
    #: Who is calling (an MCP client label, or "agent" for the in-process loop). Telemetry only.
    actor: str = "unknown"
    #: http | stdio | inproc - which door the call came through. Telemetry only.
    transport: str = "http"


_scope: contextvars.ContextVar[MCPScope | None] = contextvars.ContextVar(
    "sag_mcp_scope", default=None
)


def _require_scope() -> MCPScope:
    scope = _scope.get()
    if scope is None:
        raise RuntimeError("MCP call has no knowledge-base scope")
    return scope


@contextlib.contextmanager
def use_scope(
    engine_manager: EngineManager,
    sources: Source | Iterable[Source],
    *,
    actor: str = "unknown",
    transport: str = "http",
):
    """Bind one source, or a set of sources, inside the context."""
    if hasattr(sources, "sag_source_config_id"):
        selected = (sources,)
    else:
        selected = tuple(sources)
    token = _scope.set(
        MCPScope(
            engine_manager=engine_manager,
            sources=selected,
            actor=actor or "unknown",
            transport=transport,
        )
    )
    try:
        yield
    finally:
        _scope.reset(token)


# ── Telemetry của tool tri thức ────────────────────────────────────────────
#
# Đây là chỗ trả lời "khi vibe, agent lấy được tri thức gì và lấy như nào": mỗi lần gọi
# tool ghi lại tham số, số bằng chứng trả về, chunk_id đã chạm và độ trễ. Đo tại tool
# nên **mọi** đường vào (HTTP, stdio, vòng chạy nội bộ) đều được tính, không phải bọc
# riêng từng transport.

_CHUNK_ID_RE = re.compile(r"chunk_id=([A-Za-z0-9_\-]+)")
_DOCUMENT_ID_RE = re.compile(r"\bid=([a-f0-9]{32})\b")
#: Tham số mang "câu hỏi" của từng tool, theo thứ tự ưu tiên khi ghi log.
_QUERY_FIELDS = ("query", "pattern", "name", "document_id", "chunk_id", "source_id")
#: Kết quả rỗng của các tool đều là một dòng trong ngoặc đơn, ví dụ "(nothing matched)".
_EMPTY_RESULT_RE = re.compile(r"^\([^)\n]{0,120}\)$")
_PREVIEW_CHARS = 400


def _result_stats(text: str) -> tuple[int, list[str]]:
    """Đếm số kết quả và gom chunk_id đã trả về (dùng chính định dạng text của tool)."""
    if not text or _EMPTY_RESULT_RE.match(text.strip()):
        return 0, []
    chunk_ids = list(dict.fromkeys(_CHUNK_ID_RE.findall(text)))[:20]
    if chunk_ids:
        return len(chunk_ids), chunk_ids
    listed = len([line for line in text.splitlines() if line.startswith("- ")])
    return (listed or 1), []


def _traced(tool_name: str):
    """Bọc một tool tri thức để ghi lại lần gọi. Không đổi chữ ký (FastMCP vẫn sinh schema đúng)."""

    def decorate(func):
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> str:
            scope = _scope.get()
            started = time.monotonic()
            query = next((str(kwargs[field]) for field in _QUERY_FIELDS if kwargs.get(field)), None)
            detail: dict[str, Any] = {"args": {k: v for k, v in kwargs.items() if v not in ("", None)}}
            try:
                result = await func(*args, **kwargs)
            except Exception as error:  # noqa: BLE001 - ghi rồi ném tiếp, không nuốt
                await emit_agent_event(
                    AgentEventRecord(
                        kind="knowledge_call",
                        actor=scope.actor if scope else "unknown",
                        transport=scope.transport if scope else "http",
                        tool=tool_name,
                        query=query,
                        ok=False,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        detail=detail,
                        error=str(error)[:500],
                    )
                )
                raise
            text = result if isinstance(result, str) else str(result)
            count, chunk_ids = _result_stats(text)
            if chunk_ids:
                detail["chunk_ids"] = chunk_ids
            documents = list(dict.fromkeys(_DOCUMENT_ID_RE.findall(text)))[:20]
            if documents:
                detail["document_ids"] = documents
            detail["preview"] = text[:_PREVIEW_CHARS]
            await emit_agent_event(
                AgentEventRecord(
                    kind="knowledge_call",
                    actor=scope.actor if scope else "unknown",
                    transport=scope.transport if scope else "http",
                    tool=tool_name,
                    query=query,
                    ok=True,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    result_count=count,
                    result_chars=len(text),
                    detail=detail,
                )
            )
            return result

        return wrapper

    return decorate


def _selected_sources(scope: MCPScope, source_id: str = "") -> tuple[Source, ...]:
    target = (source_id or "").strip()
    if not target:
        return scope.sources
    return tuple(source for source in scope.sources if source.id == target)


def _source_title(source: Source) -> str:
    return f"{source.name}（source_id={source.id}）"


def _sections_to_text(sections: list, sources: tuple[Source, ...]) -> str:
    if not sections:
        return "(no related material)"
    by_config = {source.sag_source_config_id: source for source in sources}
    by_id = {source.id: source for source in sources}
    blocks = []
    for index, section in enumerate(sections, start=1):
        heading = getattr(section, "heading", None) or "Snippet"
        chunk_id = getattr(section, "chunk_id", None) or ""
        tag = f"（chunk_id={chunk_id}）" if chunk_id else ""
        source = by_config.get(getattr(section, "source_config_id", None))
        source = source or by_id.get(getattr(section, "source_id", None))
        source_line = f"Source: {_source_title(source)}\n" if source and len(sources) > 1 else ""
        blocks.append(
            f"[{index}] {heading}{tag}\n{source_line}{getattr(section, 'content', '')}"
        )
    return "\n\n".join(blocks)


async def _document_in_scope(
    scope: MCPScope, document_id: str
) -> tuple[Document, Source] | None:
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Document

    async with SessionLocal() as session:
        document = await session.get(Document, (document_id or "").strip())
    if document is None:
        return None
    source = next((item for item in scope.sources if item.id == document.source_id), None)
    return (document, source) if source is not None else None


def build_source_mcp(
    *,
    stateless_http: bool = False,
    transport_security: TransportSecuritySettings | None = None,
) -> FastMCP:
    """Build the knowledge-base MCP server; the actual scope is injected by a contextvar before each request."""
    mcp = FastMCP(
        "sag-knowledge",
        instructions=(
            "SAG knowledge-base MCP: searches every source by default, or pass source_id to a tool to narrow the scope. "
            "Start with list_sources/list_documents to learn what material exists, then use search, grep, outline, "
            "read and get_chunk to collect citable evidence. Base answers on the numbered evidence that search returns. "
            "For delegation, call list_sub_agents first and then ask_sub_agent; never infer Brain registry state from host CLIs."
        ),
        stateless_http=stateless_http,
        transport_security=transport_security,
    )

    @mcp.tool(
        title=MCP_TOOL_LABELS["list_sources"],
        description=MCP_TOOL_DESCRIPTIONS["list_sources"],
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    @_traced("list_sources")
    async def list_sources() -> str:
        scope = _require_scope()
        if not scope.sources:
            return "(the knowledge base has no sources yet)"
        return "\n".join(
            f"- {_source_title(source)} - {source.document_count} documents - {source.chunk_count} chunks"
            for source in scope.sources
        )

    @mcp.tool(
        title=MCP_TOOL_LABELS["search"],
        description=MCP_TOOL_DESCRIPTIONS["search"],
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    @_traced("search")
    async def search(
        query: Annotated[str, Field(description="The question, topic or keywords to look for.")],
        top_k: Annotated[
            int,
            Field(description="How many pieces of evidence to return at most; defaults to 8, the server clamps it to 1-50."),
        ] = 8,
        source_id: SourceId = "",
    ) -> str:
        scope = _require_scope()
        selected = _selected_sources(scope, source_id)
        if not selected:
            return "(no searchable source)" if not source_id else "(source does not exist or is outside the current scope)"
        normalized = (query or "").strip()
        if not normalized:
            return "(empty query)"
        outcome = await retrieve_relevant_sections(
            scope.engine_manager,
            selected,
            normalized,
            top_k=max(1, min(top_k, 50)),
        )
        return _sections_to_text(outcome.sections, selected)

    @mcp.tool(
        title=MCP_TOOL_LABELS["get_entity"],
        description=MCP_TOOL_DESCRIPTIONS["get_entity"],
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    @_traced("get_entity")
    async def get_entity(
        name: Annotated[
            str,
            Field(description="Entity name such as a person, organisation or concept; a full or partial name both work."),
        ],
        source_id: SourceId = "",
    ) -> str:
        scope = _require_scope()
        selected = _selected_sources(scope, source_id)
        if not selected:
            return "(no queryable source)" if not source_id else "(source does not exist or is outside the current scope)"
        target = (name or "").strip()
        if not target:
            return "(entity not found)"

        async def _one(source: Source) -> str | None:
            try:
                scid = source.sag_source_config_id
                entities = await scope.engine_manager.list_entities(
                    scid, source=source, limit=200
                )
                lowered = target.lower()
                match = next(
                    (entity for entity in entities if (entity.name or "").lower() == lowered),
                    None,
                )
                if match is None:
                    match = next(
                        (
                            entity
                            for entity in entities
                            if lowered in (entity.name or "").lower()
                        ),
                        None,
                    )
                if match is None:
                    return None
                snippets = await scope.engine_manager.entity_context(
                    scid, match.id, source=source, limit=6
                )
                body = "\n\n".join(snippets) if snippets else (match.description or "")
                prefix = f"Source: {_source_title(source)}\n" if len(selected) > 1 else ""
                return f"{prefix}Entity \"{match.name}\" ({match.type}):\n{body}".strip()
            except Exception:
                return None

        results = await asyncio.gather(*(_one(source) for source in selected))
        matches = [result for result in results if result]
        return "\n\n".join(matches) if matches else "(entity not found)"

    @mcp.tool(
        title=MCP_TOOL_LABELS["list_documents"],
        description=MCP_TOOL_DESCRIPTIONS["list_documents"],
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    @_traced("list_documents")
    async def list_documents(source_id: SourceId = "") -> str:
        scope = _require_scope()
        selected = _selected_sources(scope, source_id)
        if not selected:
            return "(the knowledge base has no sources yet)" if not source_id else "(source does not exist or is outside the current scope)"
        from sqlalchemy import select

        from sag_api.core.db import SessionLocal
        from sag_api.db.models import Document

        source_ids = [source.id for source in selected]
        async with SessionLocal() as session:
            documents = list(
                (
                    await session.execute(
                        select(Document)
                        .where(Document.source_id.in_(source_ids))
                        .order_by(Document.created_at, Document.id)
                    )
                )
                .scalars()
                .all()
            )
        if not documents:
            return "(the knowledge base has no documents yet)"
        by_source: dict[str, list[Document]] = {item.id: [] for item in selected}
        for document in documents:
            by_source.setdefault(document.source_id, []).append(document)
        blocks = []
        for source in selected:
            rows = by_source.get(source.id) or []
            if not rows:
                continue
            lines = []
            for document in rows:
                status = getattr(document.status, "value", document.status)
                lines.append(
                    f"- {document.filename} · id={document.id} · {status} · "
                    f"{document.chunk_count} chunks"
                )
            header = f"## {_source_title(source)}\n" if len(selected) > 1 else ""
            blocks.append(header + "\n".join(lines))
        return "\n\n".join(blocks)

    @mcp.tool(
        title=MCP_TOOL_LABELS["outline"],
        description=MCP_TOOL_DESCRIPTIONS["outline"],
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    @_traced("outline")
    async def outline(document_id: DocumentId) -> str:
        scope = _require_scope()
        match = await _document_in_scope(scope, document_id)
        if match is None:
            return "(document not found)"
        document, source = match
        if not document.sag_source_id:
            return "(no outline yet: the document may still be processing)"
        rows = await scope.engine_manager.list_chunk_headings(
            source.sag_source_config_id,
            source=source,
            doc_sag_id=document.sag_source_id,
        )
        if not rows:
            return "(no outline yet: the document may still be processing)"
        return "\n".join(
            f"{row['rank']:>3}. {row['heading'] or '(untitled chunk)'}"
            f"（chunk_id={row['chunk_id']}）"
            for row in rows
        )

    @mcp.tool(
        title=MCP_TOOL_LABELS["grep"],
        description=MCP_TOOL_DESCRIPTIONS["grep"],
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    @_traced("grep")
    async def grep(
        pattern: Annotated[
            str,
            Field(description="Text to match literally in the raw content; best for proper nouns, identifiers, fixed phrases and code."),
        ],
        limit: Annotated[
            int,
            Field(description="How many matches to return at most; defaults to 20, the server clamps it to 1-100."),
        ] = 20,
        source_id: SourceId = "",
    ) -> str:
        scope = _require_scope()
        selected = _selected_sources(scope, source_id)
        if not selected:
            return "(no queryable source)" if not source_id else "(source does not exist or is outside the current scope)"
        needle = (pattern or "").strip()
        if not needle:
            return "(empty match string)"
        bounded_limit = max(1, min(limit, 100))

        async def _one(source: Source) -> list[dict]:
            try:
                return await scope.engine_manager.grep_chunks(
                    source.sag_source_config_id,
                    needle,
                    source=source,
                    limit=bounded_limit,
                )
            except Exception:
                return []

        results = await asyncio.gather(*(_one(source) for source in selected))
        blocks = []
        for source, rows in zip(selected, results, strict=True):
            for row in rows:
                source_line = (
                    f"Source: {_source_title(source)}\n" if len(selected) > 1 else ""
                )
                blocks.append(
                    f"{row['heading'] or 'Snippet'} (chunk_id={row['chunk_id']})\n"
                    f"{source_line}{row['snippet']}"
                )
                if len(blocks) >= bounded_limit:
                    break
            if len(blocks) >= bounded_limit:
                break
        if not blocks:
            return "(nothing matched)"
        return "\n\n".join(f"[{index}] {block}" for index, block in enumerate(blocks, 1))

    @mcp.tool(
        title=MCP_TOOL_LABELS["read"],
        description=MCP_TOOL_DESCRIPTIONS["read"],
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    @_traced("read")
    async def read(
        document_id: DocumentId,
        offset: Annotated[
            int,
            Field(description="Line to start reading from; the first line is 1, default 1."),
        ] = 1,
        limit: Annotated[
            int,
            Field(description="How many lines to read this time; defaults to 120, the server returns at most 500."),
        ] = 120,
    ) -> str:
        scope = _require_scope()
        match = await _document_in_scope(scope, document_id)
        if match is None:
            return "(document not found)"
        document, source = match
        import os

        if not document.storage_path or not os.path.isfile(document.storage_path):
            return "(the raw file does not exist or has been cleaned up)"
        try:
            with open(document.storage_path, encoding="utf-8", errors="replace") as file:
                lines = file.readlines()
        except OSError:
            return "(failed to read the file)"
        start = max(0, offset - 1)
        page = lines[start : start + max(1, min(limit, 500))]
        if not page:
            return f"(out of range: the text has {len(lines)} lines in total)"
        body = "".join(f"{start + index + 1:>5}\t{line}" for index, line in enumerate(page))
        source_line = f"Source: {_source_title(source)}\n" if len(scope.sources) > 1 else ""
        return (
            f"{document.filename} - lines {start + 1}-{start + len(page)} / "
            f"{len(lines)} total\n{source_line}{body}"
        )

    @mcp.tool(
        title=MCP_TOOL_LABELS["get_chunk"],
        description=MCP_TOOL_DESCRIPTIONS["get_chunk"],
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    @_traced("get_chunk")
    async def get_chunk(chunk_id: ChunkId, source_id: SourceId = "") -> str:
        scope = _require_scope()
        selected = _selected_sources(scope, source_id)
        if not selected:
            return "(no queryable source)" if not source_id else "(source does not exist or is outside the current scope)"
        cid = (chunk_id or "").strip()
        if not cid:
            return "(missing chunk_id)"

        async def _one(source: Source):
            try:
                chunk = await scope.engine_manager.get_chunk(
                    source.sag_source_config_id, cid, source=source
                )
                return source, chunk
            except Exception:
                return source, None

        results = await asyncio.gather(*(_one(source) for source in selected))
        found = next(((source, chunk) for source, chunk in results if chunk is not None), None)
        if found is None:
            return "(chunk not found)"
        source, chunk = found
        heading = (chunk.heading or "").strip()
        body = f"{heading}\n\n{chunk.content}".strip() if heading else chunk.content
        return f"Source: {_source_title(source)}\n\n{body}" if len(selected) > 1 else body

    @mcp.tool(
        title=MCP_TOOL_LABELS["list_sub_agents"],
        description=MCP_TOOL_DESCRIPTIONS["list_sub_agents"],
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    async def list_sub_agents() -> str:
        from sag_api.core.db import SessionLocal

        scope = _scope.get()
        async with SessionLocal() as session:
            entries = await sub_agent_execution.list_available_sub_agents(session)
        if not entries:
            result = "(no enabled sub-agent is configured in Settings → Sub Agents)"
        else:
            lines = []
            for entry in entries:
                verified = entry["model_verified"]
                verification = "n/a" if verified is None else ("yes" if verified else "no")
                name = entry["provider_name"] or entry["display_name"]
                lines.append(
                    f"- {entry['provider']} | {name} | model={entry['model']} | "
                    f"credential_set={'yes' if entry['credential_set'] else 'no'} | "
                    f"model_verified={verification} | callable={'yes' if entry['callable'] else 'no'}"
                )
            result = "\n".join(lines)
        await emit_agent_event(
            AgentEventRecord(
                kind="sub_agent_registry",
                actor=scope.actor if scope else "unknown",
                transport=scope.transport if scope else "http",
                tool="list_sub_agents",
                ok=True,
                result_count=len(entries),
                result_chars=len(result),
                detail={"preview": result[:2000]},
            )
        )
        return result

    @mcp.tool(
        title=MCP_TOOL_LABELS["ask_sub_agent"],
        description=MCP_TOOL_DESCRIPTIONS["ask_sub_agent"],
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def ask_sub_agent(
        provider: Annotated[
            str,
            Field(
                description=(
                    "Exact provider id returned by list_sub_agents, for example "
                    "opencode-zen, gemini-cli, or custom."
                ),
                max_length=32,
            ),
        ],
        task: Annotated[
            str,
            Field(
                description="A concrete bounded analysis or review task. The sub-agent has no filesystem access.",
                max_length=8000,
            ),
        ],
        context: Annotated[
            str,
            Field(
                description="Relevant code, diff, constraints and recalled knowledge needed to answer the task.",
                max_length=24000,
            ),
        ] = "",
    ) -> str:
        from sag_api.core.db import SessionLocal

        scope = _scope.get()
        actor = scope.actor if scope else "unknown"
        transport = scope.transport if scope else "http"
        started = time.perf_counter()
        try:
            async with SessionLocal() as session:
                result = await sub_agent_execution.invoke_sub_agent(
                    session,
                    provider,
                    task,
                    context=context,
                    actor=actor,
                )
        except Exception as error:
            await emit_agent_event(
                AgentEventRecord(
                    kind="delegation",
                    actor=actor,
                    transport=transport,
                    tool=(provider or "unknown")[:64],
                    query=(task or "").strip()[:2000],
                    ok=False,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    detail={
                        "status": "failed",
                        "note": str(error)[:2000],
                        "execution_source": "brain",
                    },
                    error=str(error)[:2000],
                )
            )
            raise
        response = (
            f"{result.display_name} · {result.model}\n\n{result.content}"
        )
        await emit_agent_event(
            AgentEventRecord(
                kind="delegation",
                actor=actor,
                transport=transport,
                tool=result.provider,
                query=(task or "").strip()[:2000],
                model=result.model,
                ok=True,
                latency_ms=int((time.perf_counter() - started) * 1000),
                result_count=1,
                result_chars=len(result.content),
                detail={
                    "status": "done",
                    "note": result.content[:2000],
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "usage_source": "provider",
                    "execution_source": "brain",
                },
            )
        )
        return response

    @mcp.tool(
        title=MCP_TOOL_LABELS["log_agent_task"],
        description=MCP_TOOL_DESCRIPTIONS["log_agent_task"],
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def log_agent_task(
        agent: Annotated[
            str,
            Field(description="Which agent did the work, e.g. 'opencode-go', 'claude', 'gemini-cli', or 'self'."),
        ],
        task: Annotated[str, Field(description="One line describing the work that was delegated or done.")],
        status: Annotated[
            str,
            Field(description="started | done | failed. Use 'started' when handing over and log again when it ends."),
        ] = "done",
        model: Annotated[str, Field(description="Model the sub-agent ran, when known.")] = "",
        note: Annotated[str, Field(description="Result, verify command output, or why it failed.")] = "",
        input_tokens: Annotated[int, Field(description="Sub-agent prompt tokens, when the CLI reports them.")] = 0,
        output_tokens: Annotated[int, Field(description="Sub-agent completion tokens, when the CLI reports them.")] = 0,
        cost_usd: Annotated[float, Field(description="Sub-agent cost in USD, when the CLI reports it.")] = 0.0,
    ) -> str:
        """Ghi một lần giao việc cho sub-agent.

        Brain **không** nhìn thấy sub-agent chạy bằng CLI trên máy — nó không đi qua đây.
        Nên đường duy nhất để việc đó lên được telemetry là orchestrator tự khai báo.
        Số token/chi phí ở đây là **do agent khai**, không phải brain đo được: cột
        `cost_source` của bản ghi vì thế ghi rõ là "reported".
        """
        scope = _scope.get()
        normalized_status = (status or "done").strip().lower()
        if normalized_status not in {"started", "done", "failed"}:
            normalized_status = "done"
        label = (agent or "").strip() or "unknown"
        summary = (task or "").strip()
        if not summary:
            return "(task is required: say in one line what was delegated)"
        await emit_agent_event(
            AgentEventRecord(
                kind="delegation",
                actor=scope.actor if scope else "unknown",
                transport=scope.transport if scope else "http",
                tool=label,
                query=summary[:2000],
                model=(model or "").strip() or None,
                ok=normalized_status != "failed",
                detail={
                    "status": normalized_status,
                    "note": (note or "").strip()[:2000],
                    "input_tokens": max(0, int(input_tokens or 0)),
                    "output_tokens": max(0, int(output_tokens or 0)),
                    "cost_usd": max(0.0, float(cost_usd or 0.0)),
                    "cost_source": "reported",
                },
            )
        )
        return f"logged: {label} · {normalized_status} · {summary[:120]}"

    return mcp


_singleton: FastMCP | None = None


def get_source_mcp() -> FastMCP:
    """Return the MCP server reused by stdio and in-process calls."""
    global _singleton
    if _singleton is None:
        _singleton = build_source_mcp()
    return _singleton


async def serve_stdio(source_id: str | None = None) -> None:
    """Run the stdio server; every source is exposed when no source_id is given."""
    from sqlalchemy import select

    from sag_api.core.config import settings
    from sag_api.core.db import SessionLocal, dispose_db
    from sag_api.db.models import Source
    from sag_api.sag import EngineManager
    from sag_api.services import telemetry_service
    from sag_api.services.telemetry_service import (
        install_telemetry_store,
        uninstall_telemetry_store,
    )

    engine_manager = EngineManager(settings)
    async with SessionLocal() as session:
        statement = select(Source).order_by(Source.created_at, Source.id)
        if source_id:
            statement = statement.where(Source.id == source_id)
        sources = tuple((await session.execute(statement)).scalars().all())
    if source_id and not sources:
        raise SystemExit(f"Source does not exist: {source_id}")

    mcp = get_source_mcp()
    import os

    # Cầu nối stdio không có header để tự giới thiệu; agent khai tên qua biến môi trường
    # trong chính lệnh bridge (`docker exec -e SAG_MCP_ACTOR=claude-code …`).
    actor = os.environ.get("SAG_MCP_ACTOR", "").strip() or "mcp-stdio"
    # stdio chạy trong một process `docker exec` riêng, không đi qua lifespan FastAPI.
    # Nếu không cắm sink tại đây thì mọi `_traced(...)` và `log_agent_task` đều emit vào
    # `None`: đường HTTP có telemetry nhưng đúng đường bridge được khuyên dùng lại mất sạch.
    install_telemetry_store(SessionLocal)
    try:
        await telemetry_service.prune_now()
        with use_scope(engine_manager, sources, actor=actor, transport="stdio"):
            await mcp.run_stdio_async()
    finally:
        try:
            await engine_manager.aclose_all()
            await dispose_db()
        finally:
            uninstall_telemetry_store()


def _main() -> None:
    import os

    source_id = os.environ.get("SAG_MCP_SOURCE_ID", "").strip() or None
    asyncio.run(serve_stdio(source_id))


if __name__ == "__main__":
    _main()
