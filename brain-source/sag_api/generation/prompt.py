"""Prompts for answer generation and citation construction."""

from __future__ import annotations

from typing import Any

from sag_api.branding import DEFAULT_AGENT_NAME
from sag_api.sag import RetrievedSection


def _citation_excerpt(content: str) -> str:
    """Return a bounded source excerpt without assigning it event semantics."""
    text = " ".join(content.split())
    if not text:
        return ""
    excerpt_limit = 720
    excerpt = text[:excerpt_limit].strip()
    if len(text) > excerpt_limit:
        excerpt = excerpt.rstrip("…") + "…"
    return excerpt


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _iso_datetime(value: Any) -> str:
    if value is None:
        return ""
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat()).strip()
    return str(value).strip()


def _event_refs_by_section(events: list[Any] | None) -> dict[tuple[str, str], list[dict[str, str]]]:
    """Index traceable extracted events by source config and chunk.

    Event order comes from ``graph_for_sections`` and is preserved.  The
    composite key is required because chunk identifiers are only source-local.
    """

    grouped: dict[tuple[str, str], list[dict[str, str]]] = {}
    seen: dict[tuple[str, str], set[str]] = {}
    for event in events or []:
        source_config_id = str(_field(event, "source_config_id") or "").strip()
        chunk_id = str(_field(event, "chunk_id") or "").strip()
        event_id = str(_field(event, "id") or "").strip()
        title = " ".join(str(_field(event, "title") or "").split())
        if not source_config_id or not chunk_id or not event_id or not title:
            continue
        key = (source_config_id, chunk_id)
        if event_id in seen.setdefault(key, set()):
            continue
        seen[key].add(event_id)
        ref = {
            "id": event_id,
            "title": title[:500],
            "summary": " ".join(str(_field(event, "summary") or "").split())[:800],
            "category": " ".join(str(_field(event, "category") or "").split())[:100],
        }
        start_time = _iso_datetime(_field(event, "start_time"))
        if start_time:
            ref["start_time"] = start_time
        content = " ".join(str(_field(event, "content") or "").split())[:4000]
        if content:
            ref["content"] = content
        grouped.setdefault(key, []).append(ref)
    return grouped


_GUIDANCE = {
    "vi": (
        "[Mục tiêu bàn giao]\n"
        "- Ưu tiên giải quyết đúng vấn đề thật của người dùng và đưa ra kết quả có thể dùng ngay. Trước khi "
        "trả lời, làm rõ nội bộ mục tiêu, đối tượng, phạm vi, thời hạn, ràng buộc và tiêu chí thành công; không "
        "trình bày dài dòng quá trình suy luận ẩn.\n"
        "[Làm rõ và tiến hành]\n"
        "- Nếu thông tin còn thiếu có thể làm thay đổi đáng kể kết luận hoặc sản phẩm bàn giao, hãy hỏi gọn một "
        "lượt 1–3 câu then chốt trước khi nghiên cứu. Với thiếu sót nhỏ, nêu giả định hợp lý rồi tiếp tục.\n"
        "- Chia việc phức tạp thành các bước cần thiết và cập nhật cách làm từ từng kết quả công cụ. Khi bằng "
        "chứng mỏng, cũ hoặc mâu thuẫn, đổi truy vấn, khoảng thời gian hoặc công cụ thay vì lặp lại cùng một tìm kiếm.\n"
        "[Thời gian và sự kiện]\n"
        "- Xem thời gian là một phần của mọi sự kiện có tính thời điểm. Với yêu cầu mới nhất, gần đây, hiện tại, "
        "ngày tương đối, phiên bản, giá, chính sách hoặc lịch, gọi get_time trước rồi dùng ngày tuyệt đối, khoảng "
        "thời gian phù hợp và đúng đối tượng trong các lần tìm kiếm tiếp theo. Không lấy ngày trong hội thoại cũ, "
        "trí nhớ mô hình hoặc ví dụ của người dùng làm thời gian hiện tại.\n"
        "- Phân biệt ngày công bố, ngày xảy ra và ngày có hiệu lực. Khi kết luận phụ thuộc dữ kiện bên ngoài hoặc "
        "thay đổi theo thời gian, dùng kho tri thức hoặc công cụ phù hợp thay vì trình bày trí nhớ chưa kiểm chứng như sự thật.\n"
        "[Chiến lược bằng chứng]\n"
        "- Khi nghiên cứu bên ngoài, ưu tiên thông báo chính thức, tài liệu sản phẩm, dữ liệu gốc, tiêu chuẩn, tài "
        "liệu cơ quan quản lý và bài nghiên cứu. Nguồn thứ cấp chỉ bổ sung bối cảnh; đoạn trích tìm kiếm, trang tổng "
        "hợp và bài đăng lại không đủ để tự mình chứng minh kết luận chính.\n"
        "- Khi có thể, đối chiếu kết luận quan trọng hoặc biến động nhanh bằng ít nhất hai nguồn độc lập. Giải quyết "
        "mâu thuẫn theo độ trực tiếp, thẩm quyền, ngày công bố và mức phù hợp với khoảng thời gian mục tiêu. Nếu chỉ "
        "còn bằng chứng yếu, mâu thuẫn hoặc không truy cập được, tiếp tục nghiên cứu hoặc đánh dấu chưa kiểm chứng.\n"
        "[Dùng công cụ]\n"
        "- Chỉ dùng công cụ khi nhiệm vụ thật sự cần. Trả lời trực tiếp lời chào, cảm ơn, tạm biệt và câu hỏi danh "
        "tính. Không truy xuất cho sáng tác thuần tuý, tính toán đơn giản, dịch, viết lại hoặc tóm tắt chỉ dựa trên "
        "nội dung người dùng đã cung cấp. Tìm kiếm không thay thế việc làm rõ mơ hồ quan trọng.\n"
        "- Dùng search_context cho kho tri thức cục bộ, tệp tải lên hoặc phạm vi @; đổi góc truy vấn khi cần và chỉ "
        "dùng get_entity để phân giải thực thể và tạo truy vấn tiếp theo. Xác nhận dữ kiện chính bằng search_context "
        "hoặc công cụ có nguồn truy vết được; nêu rõ giới hạn nếu không có công cụ phù hợp.\n"
        "[Quy tắc trả lời]\n"
        "- Luôn trả lời bằng tiếng Việt, không xen ngôn ngữ khác trừ tên riêng, thuật ngữ hoặc trích dẫn nguyên văn "
        "cần thiết. Mở đầu bằng kết luận và đầu ra dùng được; phân biệt sự thật, suy luận, giả định và khoảng trống dữ liệu.\n"
        "- Chỉ số do search_context trả về mới được trích dẫn dạng [n] và phải đặt gần luận điểm tương ứng. Giữ URL "
        "từ công cụ khác dưới dạng liên kết Markdown, không tự tạo số trích dẫn. Nếu không tạo được nguồn truy vết, "
        "nêu khoảng trống bằng chứng thay vì trình bày dữ kiện như đã xác nhận.\n"
        "- Trước khi kết thúc, kiểm tra đã trả lời đúng mục tiêu thật, bằng chứng đủ mới, ngày và số chính xác, liên "
        "kết mở được và phần chưa chắc chắn đã được nêu rõ."
    ),
    "en": (
        "[Delivery objective]\n"
        "- Optimize for solving the user's real problem and delivering a directly usable result. Internally "
        "establish the goal, audience, scope, time horizon, constraints, and success criteria before deciding "
        "whether to answer, clarify, or use tools. Do not expose lengthy hidden reasoning.\n"
        "[Clarify and progress]\n"
        "- If missing information would materially change the conclusion or deliverable, ask one concise batch "
        "of 1-3 answerable questions before researching. For minor gaps, state reasonable assumptions and proceed.\n"
        "- Break complex work into necessary steps and update the approach from each tool result. If evidence is "
        "thin, stale, or conflicting, change the query, time window, or tool instead of repeating the same search.\n"
        "[Time and facts]\n"
        "- Treat time as part of every time-sensitive fact. For latest, recent, current, relative-date, version, "
        "price, policy, or schedule requests, call get_time first, then put absolute dates, an appropriate time "
        "window, and the subject into subsequent searches. Never treat an old conversation date, model memory, "
        "or a year in the user's example as the current date.\n"
        "- Distinguish publication, event, and effective dates. When factual research, analysis, comparisons, "
        "recommendations, or data depend on external or time-sensitive facts, use the best available knowledge "
        "or mounted tool instead of presenting unverified memory as fact.\n"
        "[Evidence strategy]\n"
        "- For external research, prefer first-party announcements, product documentation, original data, "
        "standards or regulator material, and research papers. Use reputable secondary reporting for context; "
        "search snippets, aggregators, and reposts cannot alone support a key claim.\n"
        "- When feasible, cross-check important or fast-changing claims with at least two independent sources. "
        "Resolve conflicts by directness, authority, publication date, and fit to the target time window. If only "
        "weak, conflicting, or inaccessible evidence remains, keep researching or mark the claim unverified; do "
        "not fill the gap with plausible model memory.\n"
        "[Tool use]\n"
        "- Use tools only when the task actually requires them. Answer greetings, thanks, farewells, and "
        "identity questions directly without retrieval. Do not retrieve for pure creation, simple arithmetic, "
        "or translation, rewriting, and summarization based only on content the user supplied. Clarify material "
        "ambiguity first; search is not a substitute for clarification.\n"
        "- Use search_context for local knowledge, uploads, or an @ scope; reformulate from another angle and use "
        "get_entity only to disambiguate entities and shape later searches. Confirm key facts with search_context "
        "or another tool that provides traceable sources. State verification limits when no suitable tool works.\n"
        "[Answer rules]\n"
        "- Answer in English without mixing in another language except where a proper noun, term, or necessary "
        "verbatim quotation requires it. Lead with the conclusion and usable output. Synthesize evidence and distinguish facts, inference, "
        "assumptions, and gaps. When a decision is needed, give options, tradeoffs, and a next step.\n"
        "- Only search_context numbers may be cited as [n], near the supported claim. Preserve URLs from "
        "other tools as Markdown links and never fabricate a numbered citation. Whenever an external search or "
        "reader tool supplies URLs, place a clickable direct source near each key external claim. If no traceable "
        "source can be formed, state the evidence gap instead of presenting the claim as confirmed.\n"
        "- Before finishing, verify that the real goal was answered, evidence is fresh enough, dates and numbers "
        "are accurate, citations open, and remaining uncertainty is explicit."
    ),
}

_TIME_RULE = {
    "vi": (
        "Bối cảnh hiện tại: múi giờ hệ thống là {timezone}. Cơ sở dữ liệu và API dùng UTC; "
        "hãy chuyển đổi sang múi giờ hệ thống khi giải thích cho người dùng. Ngày giờ hiện tại "
        "là dữ kiện động: phải gọi get_time khi nhiệm vụ có liên quan, không được đoán từ prompt, "
        "lịch sử hội thoại hoặc kiến thức của mô hình."
    ),
    "en": (
        "Current context: the configured system timezone is {timezone}. Database and API timestamps use UTC; "
        "convert them for the user. The current date and time are dynamic facts: call get_time for relevant "
        "tasks and never infer them from the prompt, conversation history, or model knowledge."
    ),
}

_IDENTITY = {
    "vi": "Tên của bạn là {name}. Dùng tên này khi người dùng hỏi bạn là ai hoặc tên gì.",
    "en": "Your name is {name}. Use this name when the user asks who you are.",
}

_USER_TEMPLATE = {
    "vi": "Nguồn:\n{context}\n\nCâu hỏi: {query}\n\nHãy trả lời dựa trên nguồn và trích dẫn bằng [số].",
    "en": "Sources:\n{context}\n\nQuestion: {query}\n\nAnswer from the sources and cite with [index].",
}


def estimate_tokens(text: str) -> int:
    """CJK-aware token estimate: CJK ~1 per character, everything else ~1 per 4 characters (matching the frontend)."""
    cjk = sum(1 for ch in text if "\u3000" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff")
    return cjk + max(0, (len(text) - cjk) + 3) // 4


def _format_context(sections: list[RetrievedSection], language: str) -> str:
    if not sections:
        return "Không có nguồn liên quan." if language == "vi" else "No relevant sources."
    blocks = []
    for i, s in enumerate(sections, start=1):
        heading = s.heading or ("Đoạn trích" if language == "vi" else "Excerpt")
        blocks.append(f"[{i}] {heading}\n{s.content}")
    return "\n\n".join(blocks)


def _identity_prompt(name: str, language: str) -> str:
    display_name = name.strip() or DEFAULT_AGENT_NAME
    return _IDENTITY[language].format(name=display_name)


def build_messages(
    query: str,
    sections: list[RetrievedSection],
    *,
    history: list[dict[str, str]] | None = None,
    language: str = "en",
    name: str = DEFAULT_AGENT_NAME,
) -> list[dict[str, str]]:
    # Ngôn ngữ chưa có bộ hướng dẫn riêng (vd 'vi') rơi về 'en', không về 'zh'.
    lang = language if language in _GUIDANCE else "en"
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": "\n\n".join((_identity_prompt(name, lang), _GUIDANCE[lang])),
        }
    ]
    if history:
        messages.extend(history)
    messages.append(
        {
            "role": "user",
            "content": _USER_TEMPLATE[lang].format(
                context=_format_context(sections, lang),
                query=query,
            ),
        }
    )
    return messages


def build_agent_messages(
    name: str,
    persona: dict[str, Any],
    query: str,
    *,
    history: list[dict[str, str]] | None = None,
    language: str = "en",
    timezone: str = "UTC",
    attachments: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Inject the Agent persona (agent-first: no pre-filled material section, retrieval happens through tools on demand)."""
    lang = language if language in _GUIDANCE else "en"
    persona = persona or {}
    parts = [_identity_prompt(name, lang)]
    system_prompt = str(persona.get("system_prompt") or "").strip()
    if system_prompt:
        parts.append(system_prompt)
    parts.append(_GUIDANCE[lang])
    parts.append(_TIME_RULE[lang].format(timezone=timezone))
    guardrails = persona.get("guardrails") or []
    if guardrails:
        prefix = "Ràng buộc: " if lang == "vi" else "Constraints: "
        parts.append(prefix + "; ".join(guardrails))
    empty_response = (persona.get("empty_response") or "").strip()
    if empty_response:
        template = (
            'Nếu sau khi truy xuất vẫn không có nguồn liên quan, hãy trả lời: "{response}"'
            if lang == "vi"
            else 'If retrieval still finds no relevant sources, reply: "{response}"'
        )
        parts.append(template.format(response=empty_response))
    messages: list[dict[str, str]] = [{"role": "system", "content": "\n\n".join(parts)}]
    if history:
        messages.extend(history)
    user_text = query
    if attachments:
        # Vision input: OpenAI-compatible content parts (images are read from disk into a data URL; older turns keep text only)
        import base64

        content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
        for att in attachments:
            path, media_type = att.get("path"), att.get("media_type", "image/png")
            if not path:
                continue
            try:
                with open(path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
            except OSError:
                continue
            content.append({"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}})
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": user_text})
    return messages


def build_prompt_preview(
    messages: list[dict[str, Any]],
    *,
    language: str = "en",
    limit: int = 6000,
) -> str:
    """Join the input context from before the run started into a readable preview.

    Multimodal messages (content is a list of parts): text parts stay as they are, images render as a placeholder (no base64 is emitted).
    """
    lines: list[str] = []
    current_user_index = next(
        (index for index in range(len(messages) - 1, -1, -1) if messages[index].get("role") == "user"),
        -1,
    )
    lang = language if language in _GUIDANCE else "en"
    history_labels = (
        {"user": "Lịch sử · Người dùng", "assistant": "Lịch sử · Trợ lý", "tool": "Lịch sử · Công cụ"}
        if lang == "vi"
        else {"user": "History · User", "assistant": "History · Assistant", "tool": "History · Tool"}
    )
    for index, m in enumerate(messages):
        role = m.get("role", "")
        if role == "system":
            label = "Chỉ dẫn hệ thống" if lang == "vi" else "System instructions"
        elif role == "user" and index == current_user_index:
            label = "Câu hỏi hiện tại" if lang == "vi" else "Current question"
        else:
            label = history_labels.get(role, role)
        content = m.get("content", "")
        if isinstance(content, list):
            texts = [p.get("text", "") for p in content if p.get("type") == "text"]
            images = sum(1 for p in content if p.get("type") == "image_url")
            image_label = f"\n[Ảnh đính kèm ×{images}]" if lang == "vi" else f"\n[Attached images ×{images}]"
            content = "\n".join(texts) + (image_label if images else "")
        lines.append(f"【{label}】\n{content}")
    text = "\n\n".join(lines)
    if len(text) > limit:
        # Keep the start of the system instructions and the tail holding the current question; compact only from the middle so
        # the transparency panel does not end up truncating this turn's real input.
        head = max(1, int(limit * 0.62))
        tail = max(1, limit - head)
        omitted = (
            "…[Đã rút gọn phần giữa của lịch sử]…"
            if lang == "vi"
            else "…[Middle history omitted]…"
        )
        text = text[:head].rstrip() + f"\n\n{omitted}\n\n" + text[-tail:].lstrip()
    return text


def build_citations(
    sections: list[RetrievedSection],
    source_refs: dict[str, dict[str, str]] | None = None,
    events: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Build the citation list deterministically from the retrieved sections (numbering matches the prompt).

    `source_refs`: {sag_source_config_id: {"id": sag source id, "name": source name}}.
    `events`: the real extracted events returned by `graph_for_sections`; joined on
    `(source_config_id, chunk_id)`, with at most three events attached per citation.
    The public `source_id` always means the **sag source id** (routable / can fetch the raw text); the engine-internal id is never leaked.
    `event_refs[].content` is the extracted event body; `snippet` only locates it in the raw text and
    is never used to infer or fabricate the event body.
    """
    citations = []
    event_refs = _event_refs_by_section(events)
    for i, s in enumerate(sections, start=1):
        snippet = _citation_excerpt(s.content)
        ref = (source_refs or {}).get(s.source_config_id or "") or {}
        citation = {
            "kind": "internal",
            "n": i,
            "chunk_id": s.chunk_id,
            "heading": s.heading,
            "snippet": snippet,
            "score": round(s.score, 4),
            "source_id": ref.get("id"),
            "source_name": ref.get("name"),
        }
        event_key = ((s.source_config_id or "").strip(), (s.chunk_id or "").strip())
        matched_events = event_refs.get(event_key, [])[:3]
        if matched_events:
            citation["event_refs"] = matched_events
        citations.append(citation)
    return citations
