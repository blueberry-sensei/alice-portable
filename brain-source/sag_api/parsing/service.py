"""Định tuyến parse tài liệu, cache, và chuyển đổi cục bộ bằng MarkItDown.

Chỉ dùng bộ chuyển đổi CHẠY CỤC BỘ. Bản gốc upstream còn một nhánh gọi dịch vụ
parse PDF của bên thứ ba (kèm cả cơ chế fallback/marker đi kèm); nhánh đó đã bị
gỡ bỏ hoàn toàn — ALICE không gửi tài liệu của người dùng ra dịch vụ ngoài.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from sag_api.core.config import Settings
from sag_api.core.errors import UpstreamError, ValidationError
from sag_api.parsing.text import TextDecodingError, is_plain_text_path, read_text_file

ParseStateCallback = Callable[[dict[str, Any]], Awaitable[None]]
_PARSE_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

#: Chỉ còn một bộ chuyển đổi, nhưng vẫn giữ chữ ký cache để cache cũ không lẫn
#: với cache mới nếu sau này thêm parser khác.
_SIGNATURE = "markitdown"


@dataclass(frozen=True, slots=True)
class PreparedDocument:
    path: str
    provider: Literal["original", "markitdown"]
    cached: bool = False


async def prepare_document(
    path: str,
    settings: Settings,
    *,
    state: dict[str, Any] | None = None,
    on_state: ParseStateCallback | None = None,
) -> PreparedDocument:
    """Trả về đường dẫn Markdown giao thẳng cho alicecore, giữ nguyên file gốc."""
    suffix = os.path.splitext(path)[1].lower()
    if suffix in {".md", ".markdown"}:
        return PreparedDocument(path=path, provider="original")

    cache_path = f"{path}.parsed.{_SIGNATURE}.md"
    if _is_cached(cache_path):
        return PreparedDocument(path=cache_path, provider="markitdown", cached=True)

    # Trong cùng một tiến trình, mỗi tài liệu chỉ chuyển đổi một lần — tránh
    # nhiều job "xử lý lại" cùng lúc làm việc trùng nhau.
    async with _lock_for(cache_path):
        if _is_cached(cache_path):
            return PreparedDocument(path=cache_path, provider="markitdown", cached=True)
        return await _prepare_and_cache(
            path, cache_path, settings, state=state, on_state=on_state
        )


async def _prepare_and_cache(
    path: str,
    cache_path: str,
    settings: Settings,
    *,
    state: dict[str, Any] | None,
    on_state: ParseStateCallback | None,
) -> PreparedDocument:
    parser_state = _compatible_state(state)
    if on_state:
        await on_state(parser_state)

    markdown = (
        await asyncio.to_thread(_convert_plain_text, path)
        if is_plain_text_path(path)
        else await _convert_with_markitdown(path)
    )

    await asyncio.to_thread(_write_markdown, cache_path, markdown)
    if on_state:
        await on_state(
            {
                **parser_state,
                "status": "done",
                "cache_path": cache_path,
            }
        )
    return PreparedDocument(path=cache_path, provider="markitdown")


def _compatible_state(state: dict[str, Any] | None) -> dict[str, Any]:
    """Giữ lại state cũ nếu vẫn khớp parser hiện tại, ngược lại bắt đầu lại."""
    expected = {"provider": "markitdown", "signature": _SIGNATURE}
    current = dict(state or {})
    if any(current.get(key) != value for key, value in expected.items()):
        return expected
    return current


def _is_cached(path: str) -> bool:
    try:
        if not os.path.isfile(path) or os.path.getsize(path) <= 0:
            return False
        if not path.lower().endswith(".md"):
            return True
        with open(path, encoding="utf-8") as cached:
            return _is_meaningful_markdown(cached.read(4096))
    except (OSError, UnicodeError):
        return False


def _lock_for(path: str) -> asyncio.Lock:
    lock = _PARSE_LOCKS.get(path)
    if lock is None:
        lock = asyncio.Lock()
        _PARSE_LOCKS[path] = lock
    return lock


def parsed_sidecar_paths(path: str) -> list[str]:
    """Liệt kê cache parse nằm cạnh file gốc, để xoá tài liệu thì dọn luôn."""
    directory = os.path.dirname(path) or "."
    prefix = os.path.basename(path) + ".parsed."
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return [os.path.join(directory, name) for name in names if name.startswith(prefix)]


async def _convert_with_markitdown(path: str) -> str:
    try:
        markdown = await asyncio.to_thread(_markitdown_sync, path)
    except (ImportError, ModuleNotFoundError) as exc:
        raise UpstreamError("Chưa cài MarkItDown nên không parse được file này") from exc
    except Exception as exc:  # noqa: BLE001 - gom lỗi của thư viện chuyển đổi
        raise ValidationError(f"MarkItDown parse thất bại: {exc}") from exc
    markdown = markdown.strip()
    if not _is_meaningful_markdown(markdown):
        raise ValidationError("MarkItDown không lấy được nội dung hợp lệ từ file")
    return markdown + "\n"


def _convert_plain_text(path: str) -> str:
    try:
        decoded = read_text_file(path)
    except TextDecodingError as exc:
        raise ValidationError(f"Không nhận diện được bảng mã văn bản: {exc}") from exc
    text = decoded.text.strip()
    if not _is_meaningful_markdown(text):
        raise ValidationError("File văn bản không có nội dung hợp lệ để parse")
    return text + "\n"


def _is_meaningful_markdown(markdown: str) -> bool:
    normalized = markdown.strip().casefold()
    return bool(normalized) and normalized not in {
        "none",
        "null",
        "undefined",
        "nan",
        "{}",
        "[]",
    }


def _markitdown_sync(path: str) -> str:
    from markitdown import MarkItDown

    result = MarkItDown().convert(path)
    markdown = getattr(result, "markdown", None)
    if markdown is None:  # tương thích 0.0.x / 0.1.x đời đầu
        markdown = getattr(result, "text_content", None)
    if not isinstance(markdown, str):
        raise TypeError("MarkItDown trả về định dạng kết quả lạ")
    return markdown


def _write_markdown(path: str, markdown: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=".parsed-", suffix=".md", dir=os.path.dirname(path) or "."
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            target.write(markdown)
        os.replace(temp_path, path)
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise
