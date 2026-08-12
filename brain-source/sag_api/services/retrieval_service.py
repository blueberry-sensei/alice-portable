"""Shared bounded retrieval, reranking, and evidence-grounded search answers."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from sag_api.core.config import settings
from sag_api.core.logging import get_logger
from sag_api.sag import RetrievedSection, SearchOutcome

log = get_logger("retrieval")


class SearchSource(Protocol):
    id: str
    name: str
    sag_source_config_id: str


EventScoreMap = dict[tuple[str, str], float]


# Chữ dẫn nhập không mang thông tin, bị gỡ trước khi đưa câu hỏi xuống tầng truy hồi.
# Bản trước chỉ liệt kê tiếng Trung (di sản upstream) nên không cắt được gì cho người dùng thật.
_QUERY_NOISE = (
    "knowledge base",
    "in the documents",
    "in the material",
    "tell me",
    "look up",
    "search for",
    "please",
    "about",
    "what is",
    "what are",
    "which",
    "kho tri thức",
    "trong tài liệu",
    "cho tôi biết",
    "tìm giúp",
    "tra giúp",
    "là gì",
    "có những gì",
    "về",
    # Cụm CJK của bản gốc, viết bằng escape: source không còn chữ Trung, nhưng tài liệu và câu
    # hỏi CJK người dùng nạp vào vẫn được làm sạch như trước — bỏ hẳn là mất tính năng.
    "\u77e5\u8bc6\u5e93",
    "\u8d44\u6599\u5e93",
    "\u8d44\u6599\u4e2d",
    "\u6587\u6863\u4e2d",
    "\u544a\u8bc9\u6211",
    "\u5e2e\u6211\u67e5",
    "\u641c\u7d22",
    "\u67e5\u8be2",
    "\u8bf7\u95ee",
    "\u5173\u4e8e",
    "\u6700\u65b0",
    "\u6700\u8fd1",
    "\u52a8\u6001",
    "\u6d88\u606f",
    "\u65b0\u95fb",
    "\u5185\u5bb9",
    "\u8d44\u6599",
    "\u4e00\u4e0b",
    "\u662f\u4ec0\u4e48",
    "\u6709\u54ea\u4e9b",
    "\u6709\u4ec0\u4e48",
)
# Rác điều hướng/chân trang của trang web nguồn. Bản cũ ghim tên vài site tiếng Trung nên chỉ
# lọc được đúng mấy site đó; nay dùng mẫu chung, áp cho mọi trang.
_BOILERPLATE = (
    "all rights reserved",
    "copyright",
    "privacy policy",
    "terms of service",
    "disclaimer",
    "load more",
    "read more",
    "most read",
    "most commented",
    "trending now",
    "sitemap",
    "cookie",
    "subscribe to our newsletter",
    "bản quyền",
    "điều khoản",
    "chính sách bảo mật",
    "xem thêm",
    "tải thêm",
    "đọc nhiều nhất",
    # Mẫu CJK của bản gốc, giữ dưới dạng escape vì lý do như `_QUERY_NOISE`.
    "\u65b0\u6d6a\u9996\u9875",
    "\u6743\u5229\u4fdd\u62a4\u58f0\u660e",
    "\u9605\u8bfb\u6392\u884c\u699c",
    "\u8bc4\u8bba\u6392\u884c\u699c",
    "\u70b9\u51fb\u52a0\u8f7d\u66f4\u591a",
    "\u514d\u8d23\u58f0\u660e",
)
_CITATION_RE = re.compile(r"\[(\d+)]")
_ASCII_WORD = re.compile(r"[a-z]", re.IGNORECASE)


def _normalized(value: str) -> str:
    # Dải CJK viết bằng escape: app không còn chữ Trung, nhưng tài liệu người dùng nạp vào thì
    # vẫn có thể là CJK — bỏ dải này là mất hẳn tín hiệu từ vựng cho các tài liệu đó.
    return "".join(re.findall(r"[a-z0-9À-ỹ\u3400-\u9fff]+", value.lower()))


def _strip_noise(text: str) -> str:
    """Gỡ cụm dẫn nhập theo BIÊN TỪ.

    Bản cũ dùng `str.replace` — hợp lý khi danh sách toàn tiếng Trung (không có khoảng trắng),
    nhưng với tiếng Anh/Việt nó ăn cả vào giữa từ ("about" trong "aboutique").
    """
    cleaned = text.strip().lower()
    for phrase in _QUERY_NOISE:
        if _ASCII_WORD.search(phrase):
            cleaned = re.sub(rf"(?<!\w){re.escape(phrase)}(?!\w)", " ", cleaned)
        else:
            # Cụm CJK không có ranh giới từ; `\w` khớp cả chữ Hán nên guard biên từ sẽ
            # không bao giờ khớp. Thay thẳng, đúng như bản gốc.
            cleaned = cleaned.replace(phrase, " ")
    return cleaned


# Hư từ không mang tín hiệu truy hồi; giữ lại thì `terms[:4]` toàn bị chúng chiếm chỗ.
_STOPWORDS = frozenset(
    """a an and are as at be by do does for from has have how in is it its of on or that the
    this to was were what when where which who why with you your
    các cái của có gì khi là làm mà một nào này những ở ra sao thì trong và vào với""".split()
)


def query_terms(query: str) -> list[str]:
    """Extract a small, deterministic lexical signal without pretending to segment words."""

    cleaned = _strip_noise(query)
    candidates = re.findall(
        r"[a-z0-9À-ỹ][a-z0-9À-ỹ_.+-]{1,31}|[\u3400-\u9fff]{2,16}",
        cleaned,
    )
    terms: list[str] = []
    for candidate in candidates:
        value = candidate.strip()
        if not value or value.isdigit() or value in _STOPWORDS or value in terms:
            continue
        terms.append(value)
    return terms[:4]


def _section_key(section: RetrievedSection) -> tuple[str, str]:
    source = (section.source_config_id or section.source_id or "").strip()
    chunk = (section.chunk_id or "").strip()
    if chunk:
        return source, chunk
    fingerprint = _normalized(f"{section.heading}\n{section.content}")[:240]
    return source, fingerprint


def _lexical_relevance(query: str, section: RetrievedSection) -> float:
    heading = _normalized(section.heading)
    content = _normalized(section.content)
    text = f"{heading}{content}"
    if not text:
        return 0.0

    terms = [_normalized(term) for term in query_terms(query)]
    terms = [term for term in terms if term]
    phrase = _normalized(_strip_noise(query))

    score = 0.0
    if phrase and len(phrase) >= 2 and phrase in text:
        score += 0.55
        if phrase in heading:
            score += 0.2
    if terms:
        matched = sum(term in text for term in terms)
        heading_matched = sum(term in heading for term in terms)
        score += 0.35 * matched / len(terms)
        score += 0.15 * heading_matched / len(terms)
    return min(1.0, score)


def _is_boilerplate(section: RetrievedSection) -> bool:
    # So khớp không phân biệt hoa thường: mẫu tiếng Trung cũ không có khái niệm này,
    # mẫu tiếng Anh/Việt thì có, và chân trang thật thường viết hoa đầu từ.
    text = f"{section.heading}\n{section.content}".lower()
    return sum(marker in text for marker in _BOILERPLATE) >= 2


@dataclass(frozen=True, slots=True)
class RerankResult:
    sections: list[RetrievedSection]
    candidate_count: int
    relevant_count: int
    filtered_count: int
    lexical_count: int


def rerank_sections(
    query: str,
    semantic: list[RetrievedSection],
    *,
    lexical: list[RetrievedSection] | None = None,
    limit: int,
) -> RerankResult:
    """Hybrid rerank with an explicit relevance gate before anything reaches an answer."""

    lexical = lexical or []
    exact_keys = {_section_key(section) for section in lexical}
    merged: dict[tuple[str, str], tuple[RetrievedSection, int]] = {}
    for index, section in enumerate([*semantic, *lexical]):
        key = _section_key(section)
        if not key[1]:
            continue
        previous = merged.get(key)
        if previous is None:
            merged[key] = (section, index)
            continue
        previous_section, previous_index = previous
        chosen = section if len(section.content.strip()) > len(previous_section.content.strip()) else previous_section
        merged[key] = (
            chosen.model_copy(update={"score": max(float(previous_section.score), float(section.score))}),
            min(previous_index, index),
        )

    candidates = list(merged.items())
    if not candidates:
        return RerankResult([], 0, 0, 0, len(lexical))

    raw_scores = [max(0.0, float(item[1][0].score or 0.0)) for item in candidates]
    top_raw = max(raw_scores, default=0.0)
    semantic_floor = max(0.35, top_raw * 0.68)
    denominator = max(1, len(candidates) - 1)
    lexical_scores = {key: _lexical_relevance(query, section) for key, (section, _index) in candidates}
    has_lexical_signal = any(key in exact_keys or score >= 0.2 for key, score in lexical_scores.items())
    ranked: list[tuple[float, float, int, RetrievedSection]] = []

    for position, (key, (section, original_index)) in enumerate(candidates):
        raw = max(0.0, min(1.0, float(section.score or 0.0)))
        lexical_score = lexical_scores[key]
        exact = key in exact_keys
        if _is_boilerplate(section) and not exact and lexical_score < 0.35:
            continue
        rank_score = 1.0 - position / denominator
        combined = min(
            1.0,
            raw * 0.5 + rank_score * 0.2 + lexical_score * 0.3 + (0.15 if exact else 0.0),
        )
        if has_lexical_signal:
            relevant = exact or lexical_score >= 0.2
        else:
            relevant = raw >= semantic_floor
        if not relevant:
            continue
        ranked.append((combined, raw, original_index, section))

    ranked.sort(key=lambda item: (-item[0], -item[1], item[2], _section_key(item[3])))
    selected = [
        section.model_copy(update={"score": round(score, 6), "rank": index})
        for index, (score, _raw, _original, section) in enumerate(ranked[: max(1, limit)])
    ]
    return RerankResult(
        sections=selected,
        candidate_count=len(candidates),
        relevant_count=len(ranked),
        filtered_count=len(candidates) - len(ranked),
        lexical_count=len(lexical),
    )


async def _lexical_sections(
    engine_manager: Any,
    sources: list[SearchSource],
    query: str,
) -> list[RetrievedSection]:
    grep_chunks = getattr(engine_manager, "grep_chunks", None)
    terms = query_terms(query)
    if not callable(grep_chunks) or not terms:
        return []

    semaphore = asyncio.Semaphore(max(1, settings.search_source_concurrency))

    async def one(source: SearchSource, term: str) -> list[RetrievedSection]:
        async with semaphore:
            try:
                rows = await grep_chunks(
                    source.sag_source_config_id,
                    term,
                    source=source,
                    limit=2,
                )
            except Exception:  # noqa: BLE001
                return []
        return [
            RetrievedSection(
                chunk_id=row.get("chunk_id"),
                heading=row.get("heading") or "Exact match",
                content=row.get("snippet") or "",
                score=max(0.8, 1.0 - index * 0.02),
                rank=index,
                source_config_id=source.sag_source_config_id,
            )
            for index, row in enumerate(rows)
        ]

    groups = await asyncio.gather(*(one(source, term) for source in sources for term in terms))
    return [section for group in groups for section in group]


async def retrieve_relevant_sections(
    engine_manager: Any,
    sources: list[SearchSource],
    query: str,
    *,
    strategy: str | None = None,
    top_k: int | None = None,
) -> SearchOutcome:
    """One retrieval contract for search UI and the Agent's search_context tool."""

    requested_limit = max(1, min(int(top_k or settings.search_top_k), 50))
    candidate_limit = min(50, max(requested_limit * 3, requested_limit + 8))
    targets = [(source.sag_source_config_id, source) for source in sources]
    outcome, lexical = await asyncio.gather(
        engine_manager.search_many(
            targets,
            query,
            strategy=strategy,
            top_k=candidate_limit,
        ),
        _lexical_sections(engine_manager, sources, query),
    )
    reranked = rerank_sections(
        query,
        outcome.sections,
        lexical=lexical,
        limit=requested_limit,
    )
    stats = {
        **outcome.stats,
        "requested_top_k": requested_limit,
        "candidate_top_k": candidate_limit,
        "candidates": reranked.candidate_count,
        "relevant": reranked.relevant_count,
        "filtered_irrelevant": reranked.filtered_count,
        "lexical_candidates": reranked.lexical_count,
        "has_more": reranked.relevant_count > len(reranked.sections),
    }
    return SearchOutcome(
        query=outcome.query or query,
        sections=reranked.sections,
        stats=stats,
    )


async def recall_event_scores(
    engine_manager: Any,
    query: str,
    sources_by_config: dict[str, SearchSource],
    *,
    limit: int | None = None,
) -> EventScoreMap:
    """Best-effort direct event recall shared by Search and the Agent.

    Chunks remain the traceable evidence path.  Event recall supplies the
    semantic result layer (title + summary) so a long document does not lose
    its extracted events merely because the best matching chunks are located
    elsewhere in the document.
    """

    search = getattr(engine_manager, "search_event_scores", None)
    if not callable(search):
        return {}
    try:
        result = await search(query, sources_by_config, limit=limit)
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001
        log.warning("Event vector recall failed, continuing with the raw chunk results: %s", error)
        return {}
    if not isinstance(result, dict):
        return {}

    scores: EventScoreMap = {}
    for raw_key, raw_score in result.items():
        if not isinstance(raw_key, tuple) or len(raw_key) != 2:
            continue
        source_config_id = str(raw_key[0] or "").strip()
        event_id = str(raw_key[1] or "").strip()
        if not source_config_id or not event_id:
            continue
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            continue
        scores[(source_config_id, event_id)] = max(0.0, min(1.0, score))
    return scores


def _best_excerpt(query: str, section: RetrievedSection, limit: int = 260) -> str:
    content = re.sub(r"\s+", " ", section.content).strip()
    if not content:
        return section.heading.strip()
    sentences = [part.strip() for part in re.split(r"(?<=[\u3002\uff01\uff1f.!?])", content) if part.strip()]
    terms = [_normalized(term) for term in query_terms(query)]
    best = max(
        sentences or [content],
        key=lambda sentence: sum(term in _normalized(sentence) for term in terms),
    )
    return best[:limit] + ("…" if len(best) > limit else "")


def _answer_language(language: str | None = None) -> str:
    selected = language or settings.sag_language
    return selected if selected in {"en", "vi"} else "en"


def fallback_search_answer(
    query: str,
    sections: list[RetrievedSection],
    *,
    language: str | None = None,
) -> str:
    if not sections:
        return ""
    lang = _answer_language(language)
    lines = [f"- {_best_excerpt(query, section)} [{index}]" for index, section in enumerate(sections[:4], 1)]
    prefix = (
        "Dựa trên bằng chứng liên quan trực tiếp đến câu hỏi:"
        if lang == "vi"
        else "Based on evidence directly relevant to the question:"
    )
    return prefix + "\n" + "\n".join(lines)


def _validated_answer(answer: str, section_count: int) -> str | None:
    text = answer.strip()
    if not text:
        return None
    references = [int(value) for value in _CITATION_RE.findall(text)]
    if not references or any(value < 1 or value > section_count for value in references):
        return None
    return text


@dataclass(frozen=True, slots=True)
class SearchAnswerUpdate:
    kind: Literal["delta", "completed"]
    text: str


def _search_answer_messages(
    query: str,
    sections: list[RetrievedSection],
    *,
    language: str | None = None,
) -> tuple[list[dict[str, str]], int]:
    lang = _answer_language(language)
    evidence_blocks: list[str] = []
    used = 0
    for index, section in enumerate(sections, 1):
        default_heading = "Tài liệu liên quan" if lang == "vi" else "Relevant source"
        block = f"[{index}] {section.heading or default_heading}\n{section.content.strip()}"
        remaining = 12000 - used
        if remaining <= 0:
            break
        block = block[:remaining]
        evidence_blocks.append(block)
        used += len(block)
    return (
        [
            {
                "role": "system",
                "content": (
                    "Bạn trả lời kết quả truy xuất. Chỉ trả lời đúng câu hỏi cụ thể của người dùng, "
                    "không tóm tắt toàn bộ tập ứng viên. Chỉ dùng bằng chứng đã cho và bỏ qua nội dung "
                    "không liên quan. Mỗi kết luận thực tế phải có [số] tương ứng từ bằng chứng. Nếu "
                    "bằng chứng thiếu, hãy nói rõ; không bổ sung kiến thức thường thức hoặc suy đoán. "
                    "Trả lời ngắn gọn, trực tiếp và hoàn toàn bằng tiếng Việt."
                    if lang == "vi"
                    else "You answer retrieval results. Address only the user's specific question; do "
                    "not summarize the candidate set. Use only the provided evidence and ignore unrelated "
                    "content. Every factual conclusion must cite the corresponding evidence [number]. "
                    "State when evidence is insufficient; do not add general knowledge or guesses. "
                    "Answer concisely, directly, and entirely in English."
                ),
            },
            {
                "role": "user",
                "content": (
                    (f"Câu hỏi: {query}\n\nBằng chứng đã xếp lại theo độ liên quan:\n"
                     if lang == "vi"
                     else f"Question: {query}\n\nEvidence reranked by relevance:\n")
                    + "\n\n".join(evidence_blocks)
                ),
            },
        ],
        len(evidence_blocks),
    )


async def synthesize_search_answer(
    query: str,
    sections: list[RetrievedSection],
    *,
    llm: Any | None,
) -> str:
    """Answer the actual question from selected evidence; never summarize the raw candidate pool."""

    language = _answer_language()
    fallback = fallback_search_answer(query, sections, language=language)
    if not sections or llm is None or not getattr(llm, "configured", False):
        return fallback

    messages, evidence_count = _search_answer_messages(query, sections, language=language)
    try:
        answer = await llm.complete(messages)
    except Exception as error:  # noqa: BLE001
        log.warning("Search answer generation failed, falling back to an evidence summary: %s", error)
        return fallback
    return _validated_answer(answer, evidence_count) or fallback


async def stream_synthesize_search_answer(
    query: str,
    sections: list[RetrievedSection],
    *,
    llm: Any | None,
) -> AsyncIterator[SearchAnswerUpdate]:
    """Yield true provider deltas followed by one citation-validated answer."""

    language = _answer_language()
    fallback = fallback_search_answer(query, sections, language=language)
    if not sections or llm is None or not getattr(llm, "configured", False):
        yield SearchAnswerUpdate(kind="completed", text=fallback)
        return

    messages, evidence_count = _search_answer_messages(query, sections, language=language)
    parts: list[str] = []
    try:
        async for delta in llm.stream_complete(messages):
            if not delta:
                continue
            parts.append(delta)
            yield SearchAnswerUpdate(kind="delta", text=delta)
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001
        log.warning("Search answer streaming failed, falling back to an evidence summary: %s", error)
        yield SearchAnswerUpdate(kind="completed", text=fallback)
        return

    answer = _validated_answer("".join(parts), evidence_count) or fallback
    yield SearchAnswerUpdate(kind="completed", text=answer)
