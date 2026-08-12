"""Agent domain logic: CRUD, bindings (sources / MCP), context resolution, multi-source fan-out conversation."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sag_api.branding import DEFAULT_AGENT_AVATAR, DEFAULT_AGENT_NAME
from sag_api.core.config import settings
from sag_api.core.errors import ConflictError, NotFoundError, ValidationError
from sag_api.db.models import Agent, AgentBinding, Message, Source, Thread
from sag_api.enums import BindingTargetType, MessageRole
from sag_api.generation import build_agent_messages, build_prompt_preview
from sag_api.generation.prompt import estimate_tokens
from sag_api.services.source_service import search_source_candidates

def _legacy_digest(value: str) -> str:
    """SHA-256 của một giá trị mặc định CŨ đã nằm trong DB người dùng.

    Các giá trị đó vốn là tiếng Trung. Không giữ nguyên văn trong source (app không còn chữ
    Trung nào), nhưng vẫn phải nhận ra được, nếu không bản cài cũ mất đường di trú.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# sha256 của tiêu đề mặc định cũ do bản tiếng Trung sinh ra.
_LEGACY_DEFAULT_TITLE_DIGESTS = {"c57c30bc05f744ab3cb81c74183f0408deb55a002422c41f30fbe3ae3d87edaa"}
_DEFAULT_TITLES = {"New chat"}


def _is_default_title(title: str) -> bool:
    return title in _DEFAULT_TITLES or _legacy_digest(title) in _LEGACY_DEFAULT_TITLE_DIGESTS
THREAD_PAGE_DEFAULT = 6
THREAD_PAGE_MAX = 100
MESSAGE_PAGE_DEFAULT = 40
MESSAGE_PAGE_MAX = 100
MESSAGE_CURSOR_MAX_LENGTH = 2048
_MESSAGE_CURSOR_ID = re.compile(r"[0-9a-f]{32}\Z")


@dataclass(frozen=True, slots=True)
class MessagePage:
    items: list[Message]
    next_cursor: str | None
    has_more: bool


def _message_cursor_scope(thread_id: str) -> str:
    return hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:24]


def _urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _urlsafe_decode(value: str) -> bytes:
    if not value:
        raise ValueError("empty base64 value")
    padded = f"{value}{'=' * (-len(value) % 4)}".encode("ascii")
    decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    if _urlsafe_encode(decoded) != value:
        raise ValueError("non-canonical base64 value")
    return decoded


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON constant: {value}")


def _encode_message_cursor(thread_id: str, message: Message) -> str:
    payload = {
        "v": 1,
        "kind": "messages",
        "scope": _message_cursor_scope(thread_id),
        "created_at": message.created_at.astimezone(UTC).isoformat(timespec="microseconds"),
        "id": message.id,
    }
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    signature = hmac.new(settings.secret_key.encode("utf-8"), raw, hashlib.sha256).digest()
    return f"{_urlsafe_encode(raw)}.{_urlsafe_encode(signature)}"


def _decode_message_cursor(thread_id: str, value: str) -> tuple[datetime, str]:
    def invalid() -> ValidationError:
        return ValidationError("Invalid message cursor", code="invalid_cursor")

    if not value or len(value) > MESSAGE_CURSOR_MAX_LENGTH or value.count(".") != 1:
        raise invalid()
    try:
        encoded_payload, encoded_signature = value.split(".", 1)
        raw = _urlsafe_decode(encoded_payload)
        signature = _urlsafe_decode(encoded_signature)
        if len(raw) > 512 or len(signature) != hashlib.sha256().digest_size:
            raise ValueError("invalid cursor size")
        expected = hmac.new(settings.secret_key.encode("utf-8"), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid cursor signature")
        payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
        expected_keys = {"v", "kind", "scope", "created_at", "id"}
        if not isinstance(payload, dict) or set(payload) != expected_keys:
            raise ValueError("invalid cursor payload")
        if (
            not isinstance(payload["v"], int)
            or isinstance(payload["v"], bool)
            or payload["v"] != 1
            or payload["kind"] != "messages"
            or payload["scope"] != _message_cursor_scope(thread_id)
            or not isinstance(payload["created_at"], str)
            or not isinstance(payload["id"], str)
            or _MESSAGE_CURSOR_ID.fullmatch(payload["id"]) is None
        ):
            raise ValueError("invalid cursor scope or values")
        created_at = datetime.fromisoformat(payload["created_at"])
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("cursor timestamp must include a timezone")
        return created_at.astimezone(UTC), payload["id"]
    except (
        UnicodeEncodeError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        raise invalid() from error


# ── CRUD ────────────────────────────────────────────────────────────
async def list_agents(session: AsyncSession) -> list[Agent]:
    rows = await session.execute(select(Agent).order_by(Agent.created_at.desc()))
    return list(rows.scalars().all())


async def get_agent(session: AsyncSession, agent_id: str) -> Agent:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise NotFoundError("Agent does not exist")
    return agent


async def create_agent(session: AsyncSession, *, name: str, avatar: str = "", persona: dict | None = None) -> Agent:
    name = name.strip()
    if not name:
        raise ValidationError("Agent name cannot be empty")
    agent = Agent(name=name, avatar=avatar or name[:1], persona=persona or {})
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


async def update_agent(
    session: AsyncSession,
    agent_id: str,
    *,
    name: str | None = None,
    avatar: str | None = None,
    persona: dict | None = None,
) -> Agent:
    agent = await get_agent(session, agent_id)
    if name is not None:
        agent.name = name
    if avatar is not None:
        agent.avatar = avatar
    if persona is not None:
        agent.persona = persona
    await session.commit()
    await session.refresh(agent)
    return agent


# sha256 của lời chào mặc định cũ (bản tiếng Trung) — xem `_legacy_digest`.
_LEGACY_DEFAULT_GREETING_DIGESTS = {"b5d3140db5d6cb98c2f1032fcf1eea97aa8745cbcc4aff6827c403ee554ad01d"}
_DEFAULT_PERSONA = {"greeting": "", "system_prompt": ""}


def _is_legacy_default_persona(persona: dict) -> bool:
    if set(persona) != {"greeting", "system_prompt"} or persona.get("system_prompt"):
        return False
    greeting = persona.get("greeting")
    return isinstance(greeting, str) and _legacy_digest(greeting) in _LEGACY_DEFAULT_GREETING_DIGESTS


def _migrate_legacy_default_agent(agent: Agent) -> bool:
    """Chỉ thay các giá trị mặc định cũ; không ghi đè phần người dùng đã sửa."""
    changed = False
    if agent.name == "sag" and agent.avatar in {"s", "S"}:
        agent.name = DEFAULT_AGENT_NAME
        agent.avatar = DEFAULT_AGENT_AVATAR
        changed = True
    if _is_legacy_default_persona(agent.persona or {}):
        agent.persona = dict(_DEFAULT_PERSONA)
        changed = True
    return changed


async def get_default_agent(session: AsyncSession) -> Agent:
    """Default agent (the out-of-the-box main conversation entry): get-or-create, idempotent."""
    agent = await session.scalar(select(Agent).where(Agent.is_default.is_(True)))
    if agent is not None:
        if _migrate_legacy_default_agent(agent):
            await session.commit()
            await session.refresh(agent)
        return agent
    agent = Agent(
        name=DEFAULT_AGENT_NAME,
        avatar=DEFAULT_AGENT_AVATAR,
        is_default=True,
        persona=dict(_DEFAULT_PERSONA),
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


async def delete_agent(session: AsyncSession, agent_id: str) -> None:
    agent = await get_agent(session, agent_id)
    await session.delete(agent)
    await session.commit()


# -- Bindings (source / MCP server) ---------------------------------
async def list_bindings(session: AsyncSession, agent: Agent) -> list[AgentBinding]:
    rows = await session.execute(select(AgentBinding).where(AgentBinding.agent_id == agent.id))
    return list(rows.scalars().all())


async def add_binding(
    session: AsyncSession,
    agent: Agent,
    *,
    target_type: BindingTargetType,
    target_id: str,
    config: dict | None = None,
) -> AgentBinding:
    config = config or {}
    if target_type == BindingTargetType.SOURCE:
        if await session.get(Source, target_id) is None:
            raise NotFoundError("Source does not exist")
    elif target_type == BindingTargetType.MCP_SERVER:
        if not (config.get("url") or config.get("command")):
            raise ValidationError("An MCP server needs either a url or a command")
        target_id = target_id or (config.get("name") or config.get("url") or "mcp")
    exists = await session.scalar(
        select(AgentBinding).where(
            AgentBinding.agent_id == agent.id,
            AgentBinding.target_type == target_type,
            AgentBinding.target_id == target_id,
        )
    )
    if exists is not None:
        raise ConflictError("That target is already bound")
    binding = AgentBinding(agent_id=agent.id, target_type=target_type, target_id=target_id, config=config)
    session.add(binding)
    await session.commit()
    await session.refresh(binding)
    return binding


async def remove_binding(session: AsyncSession, agent: Agent, binding_id: str) -> None:
    binding = await session.get(AgentBinding, binding_id)
    if binding is None or binding.agent_id != agent.id:
        raise NotFoundError("Binding does not exist")
    await session.delete(binding)
    await session.commit()


async def resolve_sources(
    session: AsyncSession,
    agent: Agent,
    source_ids: list[str] | None = None,
) -> list[Source]:
    """Resolve the sources visible in this turn.

    An explicit `source_ids` comes from the @ scope in the input box and wins over persistent bindings; every
    entry point shares one candidate cap, so a default Agent or a pile of bindings cannot cause unbounded fan-out.
    """
    if source_ids:
        return await search_source_candidates(session, source_ids)
    if agent.is_default:
        return await search_source_candidates(session)
    bindings = await list_bindings(session, agent)
    src_ids = [b.target_id for b in bindings if b.target_type == BindingTargetType.SOURCE]
    if not src_ids:
        return []
    return await search_source_candidates(session, src_ids)


async def resolve_mcp_specs(session: AsyncSession, agent: Agent) -> list[tuple[str, dict]]:
    """Expand external MCP server bindings into `[(label, config), ...]`, for the agent to mount as an MCP client."""
    bindings = await list_bindings(session, agent)
    specs: list[tuple[str, dict]] = []
    for b in bindings:
        if b.target_type != BindingTargetType.MCP_SERVER:
            continue
        cfg = b.config or {}
        specs.append((cfg.get("name") or b.target_id or "mcp", cfg))
    return specs


# -- Threads --------------------------------------------------------
async def list_threads(
    session: AsyncSession,
    agent_id: str,
    *,
    archived: bool = False,
    limit: int = THREAD_PAGE_DEFAULT,
    offset: int = 0,
) -> list[Thread]:
    statement = (
        select(Thread)
        .where(Thread.agent_id == agent_id, Thread.archived.is_(archived))
        .order_by(Thread.updated_at.desc(), Thread.id.desc())
    )
    if offset:
        statement = statement.offset(offset)
    statement = statement.limit(max(1, min(int(limit), THREAD_PAGE_MAX)))
    rows = await session.execute(statement)
    return list(rows.scalars().all())


async def update_thread(
    session: AsyncSession,
    agent_id: str,
    thread_id: str,
    *,
    title: str | None = None,
    archived: bool | None = None,
) -> Thread:
    thread = await get_thread(session, agent_id, thread_id)
    if title is not None and title.strip():
        thread.title = title.strip()[:200]
    if archived is not None:
        thread.archived = archived
    await session.commit()
    await session.refresh(thread)
    return thread


async def create_thread(session: AsyncSession, agent: Agent, title: str = "New chat") -> Thread:
    thread = Thread(agent_id=agent.id, title=title or "New chat")
    session.add(thread)
    await session.commit()
    await session.refresh(thread)
    return thread


async def get_thread(session: AsyncSession, agent_id: str, thread_id: str) -> Thread:
    thread = await session.get(Thread, thread_id)
    if thread is None or thread.agent_id != agent_id:
        raise NotFoundError("Thread does not exist")
    return thread


async def delete_thread(session: AsyncSession, agent_id: str, thread_id: str) -> None:
    thread = await get_thread(session, agent_id, thread_id)
    await session.delete(thread)
    await session.commit()


async def list_messages_page(
    session: AsyncSession,
    thread_id: str,
    *,
    limit: int = MESSAGE_PAGE_DEFAULT,
    cursor: str | None = None,
) -> MessagePage:
    """Return the most recent page of messages, keeping forward chronological order within the page.

    The database does a keyset read ordered by `(created_at, id)` descending and takes only `limit + 1`
    to tell whether older messages remain; there is no COUNT scan.
    """
    if limit < 1 or limit > MESSAGE_PAGE_MAX:
        raise ValidationError(
            f"Message page size must be between 1 and {MESSAGE_PAGE_MAX}",
            code="invalid_page_limit",
        )

    statement = select(Message).where(Message.thread_id == thread_id)
    if cursor:
        created_at, message_id = _decode_message_cursor(thread_id, cursor)
        statement = statement.where(
            or_(
                Message.created_at < created_at,
                and_(Message.created_at == created_at, Message.id < message_id),
            )
        )
    rows = await session.execute(statement.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit + 1))
    candidates = list(rows.scalars().all())
    has_more = len(candidates) > limit
    page_desc = candidates[:limit]
    next_cursor = _encode_message_cursor(thread_id, page_desc[-1]) if has_more and page_desc else None
    return MessagePage(
        items=list(reversed(page_desc)),
        next_cursor=next_cursor,
        has_more=has_more,
    )


async def _history(session: AsyncSession, thread_id: str, exclude_id: str) -> list[dict[str, str]]:
    rows = await session.execute(
        select(Message)
        .where(
            Message.thread_id == thread_id,
            Message.id != exclude_id,
            Message.role.in_((MessageRole.USER, MessageRole.ASSISTANT)),
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(settings.history_load_limit)
    )
    messages = list(reversed(rows.scalars().all()))
    return [{"role": m.role.value, "content": m.content} for m in messages]


def _history_tokens(history: list[dict[str, str]]) -> int:
    return sum(estimate_tokens(m["content"]) for m in history)


async def compress_history(history: list[dict[str, str]], *, llm=None, budget_tokens: int) -> list[dict[str, str]]:
    """Context-threshold compaction: when over budget, older messages are folded into one summary and only the most recent N stay verbatim.

    With an LLM -> summarise the old span (keeping facts / conclusions / forms of address / to-dos); without an LLM, or on failure -> trim from the tail to fit the budget.
    """
    if _history_tokens(history) <= budget_tokens:
        return history

    keep = max(2, settings.history_keep_recent)
    recent = history[-keep:]
    older = history[:-keep]
    if not older:
        return recent

    if llm is not None and getattr(llm, "configured", False):
        language = settings.sag_language if settings.sag_language in {"en", "vi"} else "en"
        user_label, assistant_label = (
            ("Người dùng", "Trợ lý") if language == "vi" else ("User", "Assistant")
        )
        transcript = "\n".join(
            f"{user_label if m['role'] == 'user' else assistant_label}: {m['content']}"
            for m in older
        )[:12000]
        summary_instruction = (
            "Tóm tắt hội thoại sau thành các ý chính (không quá 400 từ). Giữ lại sự kiện, "
            "kết luận, số liệu, cách xưng hô và việc chưa giải quyết; không bình luận."
            if language == "vi"
            else "Summarize the following conversation in at most 400 words. Preserve facts, "
            "conclusions, numbers, names, forms of address, and unresolved items; do not comment."
        )
        try:
            summary = await llm.complete(
                [
                    {
                        "role": "system",
                        "content": summary_instruction,
                    },
                    {"role": "user", "content": transcript},
                ]
            )
            summary_label = (
                "Tóm tắt hội thoại trước đó để tham khảo"
                if language == "vi"
                else "Summary of the earlier conversation for reference"
            )
            return [
                {"role": "user", "content": f"{summary_label}\n{summary.strip()}"},
                *recent,
            ]
        except Exception:  # noqa: BLE001
            pass

    # Fallback: load from the most recent backwards until the budget is full
    trimmed: list[dict[str, str]] = []
    used = 0
    for m in reversed(history):
        t = estimate_tokens(m["content"])
        if used + t > budget_tokens and trimmed:
            break
        trimmed.append(m)
        used += t
    return list(reversed(trimmed))


# -- Question-answering plan ------------------------------------------
@dataclass
class AskPlan:
    """Prompt plan for one question (agent-first: retrieval happens through in-loop tools on demand, no pre-filled material section)."""

    query: str = ""
    messages: list[dict[str, str]] = field(default_factory=list)
    citations: list[dict] = field(default_factory=list)
    prompt_preview: str = ""
    source_ids: list[str] | None = None  # @ scope: limits which sources the in-loop retrieval tools can see
    user_message_id: str | None = None


def build_ask_context(
    *,
    agent: Agent,
    query: str,
    history: list[dict[str, str]] | None = None,
    attachments: list[dict] | None = None,
    source_ids: list[str] | None = None,
) -> AskPlan:
    """Assemble the messages with the system prompt (not persisted, shared by conversation and the OpenAI endpoint). The model decides through tools whether to retrieve."""
    messages = build_agent_messages(
        agent.name,
        agent.persona or {},
        query,
        history=history,
        language=settings.sag_language,
        timezone=settings.timezone,
        attachments=attachments,
    )
    return AskPlan(
        query=query,
        messages=messages,
        prompt_preview=build_prompt_preview(messages, language=settings.sag_language),
        source_ids=source_ids or None,
    )


async def prepare_ask(
    session: AsyncSession,
    *,
    agent: Agent,
    thread: Thread,
    query: str,
    attachments: list[str] | None = None,
    source_ids: list[str] | None = None,
    llm=None,
) -> AskPlan:
    """Persist the user message (including image attachment meta), resolve history (compacting when over the context threshold), and assemble the plan."""
    from sag_api.api.v1.attachments import attachment_path

    resolved: list[dict] = []
    for aid in attachments or []:
        path = attachment_path(aid)
        if path is None:
            raise ValidationError(f"Attachment does not exist or has expired: {aid}")
        ext = aid.rsplit(".", 1)[-1].lower()
        media_type = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "webp": "image/webp",
            "gif": "image/gif",
        }.get(ext, "image/png")
        resolved.append({"id": aid, "media_type": media_type, "path": path})

    user_msg = Message(
        thread_id=thread.id,
        role=MessageRole.USER,
        content=query,
        citations=[],
        attachments=[{k: a[k] for k in ("id", "media_type")} for a in resolved],
    )
    session.add(user_msg)
    if _is_default_title(thread.title):
        thread.title = query[:40] or "Image conversation"
    await session.commit()
    await session.refresh(user_msg)

    history = await _history(session, thread.id, exclude_id=user_msg.id)
    # History budget = 40% of the context window (the rest is left for tool rounds and the answer)
    history = await compress_history(history, llm=llm, budget_tokens=int(settings.llm_context_window * 0.4))
    plan = build_ask_context(
        agent=agent,
        query=query,
        history=history,
        attachments=resolved or None,
        source_ids=source_ids,
    )
    plan.user_message_id = user_msg.id
    return plan


async def delete_message(session: AsyncSession, agent_id: str, thread_id: str, message_id: str) -> None:
    thread = await get_thread(session, agent_id, thread_id)
    message = await session.get(Message, message_id)
    if message is None or message.thread_id != thread.id:
        raise NotFoundError("Message does not exist")
    await session.delete(message)
    await session.commit()


async def persist_answer(
    session_factory: async_sessionmaker,
    thread_id: str,
    answer: str,
    citations: list[dict],
    steps: list[dict] | None = None,
    prompt_preview: str = "",
) -> str:
    async with session_factory() as session:
        message = Message(
            thread_id=thread_id,
            role=MessageRole.ASSISTANT,
            content=answer,
            citations=citations,
            steps=steps or [],
            prompt_preview=prompt_preview,
        )
        session.add(message)
        await session.commit()
        await session.refresh(message)
        return message.id
