"""Runtime model and knowledge-base configuration - the DB is the single source of truth and overrides the `settings` singleton.

"Model and retrieval" configuration lives in the `settings` table (scope=global, key=model_config). At startup and after each save it
**overrides the `settings` singleton in place**, then the endpoint rebuilds `LLMClient` / resets warm engines, so a change **takes effect without a restart**.

Two important rules:

1. **The LLM can only be configured in the UI.** Credentials live in `llm_providers` (a priority chain); the `SAG_LLM_*` environment
   variables no longer take part - at startup, if the DB holds no chain, the flat credentials are **cleared**, so there is never
   an "old key still quietly active in .env" second source of truth.
2. **api_key is encrypted at rest** (AES-GCM, see `core/crypto.py`); reads only return the `api_key_set` boolean.
"""

from __future__ import annotations

from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sag_api.core.config import ENV_BASELINE, ENV_PROVIDED_FIELDS, Settings, env_var_name
from sag_api.core.config import same_endpoint as _same_endpoint
from sag_api.core.config import settings as _settings
from sag_api.core.crypto import decrypt_secret, encrypt_secret
from sag_api.core.errors import ConfigurationError
from sag_api.core.logging import get_logger
from sag_api.core.model_providers import get_model_provider
from sag_api.db.models import Setting
from sag_api.enums import SEARCH_STRATEGIES, normalize_search_strategy

_SCOPE = "global"
_KEY = "model_config"
_PREFERENCES_KEY = "system_preferences"
_SUB_AGENTS_KEY = "sub_agent_config"
log = get_logger("settings")

# Fields that may be overridden at runtime (values already validated / coerced by the request schema)
_FIELDS = frozenset(
    {
        "llm_providers",
        "llm_temperature",
        "llm_max_tokens",
        "llm_context_window",
        "llm_timeout_ms",
        "llm_max_retries",
        "embedding_model",
        "embedding_base_url",
        "embedding_api_key",
        "embedding_dimensions",
        "document_parser",
        "document_extract_concurrency",
        "document_chunk_max_tokens",
        "document_chunk_mode",
        "search_strategy",
        "search_top_k",
        "sag_language",
    }
)
_SECRET_FIELDS = frozenset({"embedding_api_key"})
_NULLABLE_FIELDS = frozenset({"embedding_base_url", "embedding_dimensions"})

#: Field nào đang lấy giá trị từ DB. Cập nhật mỗi lần `apply_overrides` chạy, để tầng API trả lời
#: được câu hỏi "giá trị này đến từ đâu" mà không phải đọc lại DB.
_DB_PROVIDED_FIELDS: set[str] = set()

_OPENAI_COMPATIBLE = get_model_provider("openai")

DEFAULT_PRESET = {
    "llm_temperature": _OPENAI_COMPATIBLE.default_temperature,
    "llm_max_tokens": 20_000,
    "llm_context_window": _OPENAI_COMPATIBLE.default_context_window,
    "llm_timeout_ms": 60_000,
    "llm_max_retries": 2,
    "embedding_dimensions": 1024,
    "document_parser": "markitdown",
    "document_extract_concurrency": 5,
    "document_chunk_max_tokens": 1_000,
    "document_chunk_mode": "standard",
    "search_strategy": "vector",
    "search_top_k": 8,
    "sag_language": "en",
}



async def _load_row(session: AsyncSession, key: str = _KEY) -> Setting | None:
    return await session.scalar(select(Setting).where(Setting.scope == _SCOPE, Setting.key == key))


def _normalize_overrides(overrides: dict) -> dict:
    """Clean up persisted configuration so a retired or invalid strategy never reaches the runtime."""
    normalized = dict(overrides)
    # Bản cũ ghi `None` cho field nullable khi người dùng xoá ô đó, và giá trị null ấy ghi đè cả
    # `.env`. Nay "không có khoá" mới là cách diễn đạt "không ghi đè", nên null tồn đọng được coi
    # đúng như vậy — bản đang cài tự khỏi mà không cần thao tác tay.
    for field in _NULLABLE_FIELDS:
        if field in normalized and normalized[field] is None:
            normalized.pop(field)
    strategy = normalized.get("search_strategy")
    if strategy == "atomic":
        normalized["search_strategy"] = normalize_search_strategy(strategy)
        log.warning("Legacy retrieval strategy atomic has been migrated to precise mode multi")
    elif strategy is not None and strategy not in SEARCH_STRATEGIES:
        normalized.pop("search_strategy", None)
        log.warning("Ignoring invalid persisted retrieval strategy: %s", strategy)
    return normalized


def _provider_entries(overrides: dict) -> list[dict]:
    raw = overrides.get("llm_providers")
    return [dict(entry) for entry in raw] if isinstance(raw, list) else []


def _encrypt_entries(entries: list[dict], previous: list[dict]) -> list[dict]:
    """Mã hoá key của từng entry trước khi ghi DB; entry gửi key rỗng thì giữ key cũ theo `id`."""
    kept = {entry.get("id"): entry.get("api_key") for entry in previous if entry.get("api_key")}
    prepared: list[dict] = []
    for entry in entries:
        item = dict(entry)
        submitted = (item.get("api_key") or "").strip()
        if submitted:
            item["api_key"] = encrypt_secret(submitted, _settings.secret_key)
        else:
            # Không gửi key mới → giữ nguyên ciphertext cũ. Đây là điều kiện để UI có thể
            # sửa nhãn / thứ tự / model mà không phải nhập lại key.
            existing = kept.get(item.get("id"))
            if existing:
                item["api_key"] = existing
            else:
                item.pop("api_key", None)
        prepared.append(item)
    return prepared


def _decrypt_entries(entries: list[dict]) -> list[dict]:
    """Giải mã key để dùng lúc chạy; entry nào không giải được thì **tắt** và nêu lý do."""
    resolved: list[dict] = []
    for entry in entries:
        item = dict(entry)
        stored = item.get("api_key") or ""
        if stored:
            plain = decrypt_secret(stored, _settings.secret_key)
            if plain is None:
                log.error(
                    "Provider %s: không giải mã được API key → tạm tắt, cần nhập lại trên UI",
                    item.get("id"),
                )
                item["enabled"] = False
                item["api_key"] = ""
                item["error"] = "credential_undecryptable"
            else:
                item["api_key"] = plain
        resolved.append(item)
    return resolved


def _masked_entries(entries: list[dict]) -> list[dict]:
    """Bản cho client: không bao giờ trả key (kể cả ciphertext), chỉ trả đã đặt hay chưa."""
    masked: list[dict] = []
    for entry in entries:
        item = {key: value for key, value in entry.items() if key != "api_key"}
        item["api_key_set"] = bool(entry.get("api_key"))
        masked.append(item)
    return masked


def _sync_flat_head(settings: Settings, entries: list[dict]) -> None:
    """Đồng bộ entry ưu tiên cao nhất vào các trường `llm_*` phẳng.

    Nhiều nơi trong ứng dụng chỉ cần biết "đang dùng model nào" (capabilities, litellm policy,
    embedding tái dùng credential). Cho chúng đọc ảnh chiếu của đầu chuỗi thay vì bắt mọi chỗ
    hiểu khái niệm chuỗi. Chuỗi rỗng → **xoá sạch** credential phẳng để env không thể lén tác dụng.
    """
    chain = sorted(
        (e for e in entries if e.get("enabled", True) and e.get("api_key") and e.get("model")),
        key=lambda e: e.get("priority", 100),
    )
    if not chain:
        settings.llm_api_key = None
        settings.llm_model = ""
        settings.llm_base_url = None
        return
    head = chain[0]
    settings.llm_provider = head.get("provider") or _OPENAI_COMPATIBLE.id
    settings.llm_api_key = head.get("api_key")
    settings.llm_model = head.get("model") or ""
    settings.llm_base_url = head.get("base_url") or None
    if head.get("extra_body"):
        settings.llm_extra_body = dict(head["extra_body"])


async def load_overrides(session: AsyncSession) -> dict:
    row = await _load_row(session)
    raw = dict(row.value) if row and isinstance(row.value, dict) else {}
    return _normalize_overrides(raw)


async def stored_provider_key(
    session: AsyncSession,
    provider_id: str,
    *,
    provider: str,
    base_url: str | None,
) -> str | None:
    """Key (đã giải mã) của một provider đã lưu — dùng cho nút Test khi form không nhập lại key.

    Chỉ trả key khi entry đang thử **vẫn trỏ đúng chỗ cũ** (cùng provider, cùng base_url).
    Nếu không có ràng buộc này thì bất cứ ai gọi được API cũng bảo server gửi key đã lưu
    tới một host tuỳ ý — biến "API không bao giờ trả key ra" thành lời hứa suông.
    """
    row = await _load_row(session)
    stored = dict(row.value) if row and isinstance(row.value, dict) else {}
    for entry in _provider_entries(stored):
        if entry.get("id") != provider_id or not entry.get("api_key"):
            continue
        if entry.get("provider") != provider or not _same_endpoint(entry.get("base_url"), base_url):
            log.warning(
                "Từ chối tái dùng key của provider %s: endpoint gửi lên khác endpoint đã lưu",
                provider_id,
            )
            return None
        return decrypt_secret(str(entry["api_key"]), _settings.secret_key)
    return None


async def model_setup_status(session: AsyncSession) -> dict[str, bool]:
    """Decide whether a first-time model configuration is needed.

    Only the DB counts: the LLM can only be configured in the UI and environment variables are no longer a valid path, so "configured" means
    the database holds at least one enabled provider that has a key.
    """
    row = await _load_row(session)
    stored = dict(row.value) if row and isinstance(row.value, dict) else {}
    entries = _provider_entries(stored)
    database_configured = any(
        entry.get("enabled", True) and entry.get("api_key") and entry.get("model") for entry in entries
    )
    return {
        "required": not database_configured,
        "environment_configured": False,
        "database_configured": database_configured,
    }


def config_sources() -> dict[str, dict[str, object]]:
    """Mỗi field cấu hình đang lấy giá trị TỪ ĐÂU, và env có đang bị bỏ qua không.

    Một tham số hai nguồn mà không ai báo là loại lỗi tốn cả buổi sáng: người dùng sửa `.env`,
    restart, rồi chờ một thay đổi không bao giờ tới, vì DB thắng trong im lặng. Hàm này biến
    thứ tự ưu tiên thành dữ liệu nhìn thấy được — cho log lúc khởi động và cho UI.
    """
    sources: dict[str, dict[str, object]] = {}
    for field in sorted(_FIELDS):
        from_db = field in _DB_PROVIDED_FIELDS
        from_env = field in ENV_PROVIDED_FIELDS
        sources[field] = {
            "source": "database" if from_db else ("environment" if from_env else "default"),
            "env_var": env_var_name(field),
            "env_set": from_env,
            # Env có giá trị nhưng DB đang thắng → mọi lần sửa `.env` đều vô hiệu cho tới khi
            # xoá giá trị trong DB (Settings → Models).
            "env_ignored": from_env and from_db,
        }
    return sources


def shadowed_env_fields() -> list[str]:
    """Field mà env đã đặt nhưng DB đang ghi đè (env đang bị bỏ qua)."""
    return sorted(field for field in _FIELDS if field in ENV_PROVIDED_FIELDS and field in _DB_PROVIDED_FIELDS)


def log_config_sources() -> None:
    """In nguồn cấu hình đang có hiệu lực. Gọi một lần lúc khởi động, sau khi đã áp override."""
    for field in sorted(_FIELDS):
        if field in _DB_PROVIDED_FIELDS:
            origin = "database (settings.model_config)"
        elif field in ENV_PROVIDED_FIELDS:
            origin = f"environment ({env_var_name(field)})"
        else:
            continue
        log.info("Config %s <- %s", field, origin)

    shadowed = shadowed_env_fields()
    if shadowed:
        log.warning(
            "The database overrides these environment variables, so editing .env changes nothing "
            "until the stored value is cleared in Settings -> Models: %s",
            ", ".join(env_var_name(field) for field in shadowed),
        )


def apply_overrides(settings: Settings, overrides: dict) -> None:
    """Write the stored overrides back into the settings singleton in place (the request schema already guarantees valid types).

    `llm_providers` is decrypted before being written (the runtime needs plaintext), and the flat `llm_*` head fields are kept in sync.
    """
    normalized = _normalize_overrides(overrides)
    # Dựng lại trạng thái ĐẦY ĐỦ từ (môi trường + override đang lưu), không chỉ chồng thêm.
    # Nhờ vậy một field bị gỡ khỏi override sẽ quay về giá trị môi trường ngay trong process này,
    # thay vì giữ giá trị cũ tới lần restart.
    for key in _FIELDS:
        if key == "llm_providers" or key in normalized:
            continue
        if key in ENV_BASELINE:
            setattr(settings, key, ENV_BASELINE[key])

    _DB_PROVIDED_FIELDS.clear()
    _DB_PROVIDED_FIELDS.update(key for key in normalized if key in _FIELDS)
    # Chuỗi provider LUÔN do DB quyết định, kể cả khi rỗng: `_sync_flat_head` xoá sạch credential
    # phẳng để env không thể lén tác dụng. Ghi vào đây cho đúng với thứ đang thực sự xảy ra.
    _DB_PROVIDED_FIELDS.add("llm_providers")
    for key, value in normalized.items():
        if key not in _FIELDS or key == "llm_providers":
            continue
        if key in _SECRET_FIELDS and isinstance(value, str) and value:
            plain = decrypt_secret(value, settings.secret_key)
            if plain is None:
                log.error("Không giải mã được %s → coi như chưa đặt", key)
                setattr(settings, key, None)
                continue
            setattr(settings, key, plain)
            continue
        setattr(settings, key, value)

    entries = _decrypt_entries(_provider_entries(normalized))
    settings.llm_providers = entries
    _sync_flat_head(settings, entries)


async def apply_startup_overrides(session_factory: async_sessionmaker) -> None:
    """At startup: apply the model configuration from the DB onto the settings singleton (call before building LLMClient)."""
    async with session_factory() as session:
        row = await _load_row(session)
        raw = dict(row.value) if row and isinstance(row.value, dict) else {}
        overrides = _normalize_overrides(raw)
        if row is not None and overrides != raw:
            # The JSON column does not use MutableDict, so the whole value must be reassigned to persist reliably.
            row.value = overrides
            await session.commit()
        apply_overrides(_settings, overrides)
        preferences = await _load_row(session, _PREFERENCES_KEY)
        preference_values = dict(preferences.value) if preferences and isinstance(preferences.value, dict) else {}
        timezone = preference_values.get("timezone")
        if isinstance(timezone, str):
            # Stored values were validated on write. Settings assignment is kept
            # explicit so model configuration and presentation preferences remain separate.
            try:
                ZoneInfo(timezone)
            except (ZoneInfoNotFoundError, ValueError):
                log.warning("Ignoring invalid persisted time zone: %s", timezone)
            else:
                _settings.timezone = timezone
    log_config_sources()


def effective_model_config() -> dict:
    """The model configuration currently in effect (reads the settings singleton; secrets are masked to *_set booleans)."""
    return {
        "llm_providers": _masked_entries(_settings.llm_providers),
        # Ảnh chiếu của entry đầu chuỗi — chỉ để hiển thị "đang dùng gì", không phải nơi cấu hình.
        "llm_active_provider": _settings.llm_provider,
        "llm_active_model": _settings.llm_model,
        "llm_temperature": _settings.llm_temperature,
        "llm_max_tokens": _settings.llm_max_tokens,
        "llm_context_window": _settings.llm_context_window,
        "llm_timeout_ms": _settings.llm_timeout_ms,
        "llm_max_retries": _settings.llm_max_retries,
        "llm_configured": _settings.llm_configured,
        "embedding_model": _settings.embedding_model,
        "embedding_base_url": _settings.embedding_base_url,
        "embedding_dimensions": _settings.embedding_dimensions,
        "embedding_api_key_set": bool(_settings.embedding_api_key),
        "document_parser": _settings.document_parser,
        "effective_document_parser": _settings.effective_document_parser,
        "document_extract_concurrency": _settings.document_extract_concurrency,
        "document_chunk_max_tokens": _settings.document_chunk_max_tokens,
        "document_chunk_mode": _settings.document_chunk_mode,
        "search_strategy": _settings.search_strategy,
        "search_top_k": _settings.search_top_k,
        "sag_language": _settings.sag_language,
        # Nguồn của từng field + cờ "env đang bị bỏ qua". UI dựa vào đây để nói thẳng với người
        # dùng rằng sửa `.env` sẽ không có tác dụng, thay vì để họ tự đoán.
        "config_sources": config_sources(),
    }


def portable_model_config_with_secrets() -> dict:
    """Ảnh cấu hình đầy đủ để mã hoá thành bundle portable bên trong API process.

    Hàm này không được dùng làm response trực tiếp: dict trả về chứa plaintext API key.
    """
    config = {
        key: getattr(_settings, key)
        for key in _FIELDS
        if key not in {"llm_providers", "embedding_api_key"}
    }
    config["llm_providers"] = [dict(entry) for entry in _settings.llm_providers]
    config["embedding_api_key"] = _settings.embedding_api_key
    return config


async def get_system_preferences(session: AsyncSession) -> dict[str, str | bool]:
    row = await _load_row(session, _PREFERENCES_KEY)
    stored = dict(row.value) if row and isinstance(row.value, dict) else {}
    timezone = stored.get("timezone")
    configured = False
    if isinstance(timezone, str):
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            timezone = None
        else:
            configured = True
    return {
        "timezone": timezone if isinstance(timezone, str) else _settings.timezone,
        "timezone_configured": configured,
    }


async def save_system_preferences(session: AsyncSession, patch: dict) -> dict[str, str | bool]:
    row = await _load_row(session, _PREFERENCES_KEY)
    stored = dict(row.value) if row and isinstance(row.value, dict) else {}
    timezone = patch.get("timezone")
    if isinstance(timezone, str):
        stored["timezone"] = timezone

    if row is None:
        session.add(Setting(scope=_SCOPE, key=_PREFERENCES_KEY, value=stored))
    else:
        row.value = stored
    await session.commit()

    if isinstance(stored.get("timezone"), str):
        _settings.timezone = stored["timezone"]
    return {
        "timezone": _settings.timezone,
        "timezone_configured": isinstance(stored.get("timezone"), str),
    }


def _sub_agent_entries(row: Setting | None) -> list[dict]:
    stored = dict(row.value) if row and isinstance(row.value, dict) else {}
    raw = stored.get("entries")
    return [dict(entry) for entry in raw] if isinstance(raw, list) else []


def _masked_sub_agent_entries(entries: list[dict]) -> list[dict]:
    """Không trả plaintext/ciphertext credential; key hỏng được báo để UI xin nhập lại."""
    masked: list[dict] = []
    for entry in entries:
        item = {key: value for key, value in entry.items() if key != "credential"}
        stored = entry.get("credential")
        credential_ok = False
        if isinstance(stored, str) and stored:
            credential_ok = decrypt_secret(stored, _settings.secret_key) is not None
        item["credential_set"] = credential_ok
        if entry.get("provider") != "custom":
            item["model_verified"] = bool(item.get("model_verified") and credential_ok)
        if stored and not credential_ok:
            item["error"] = "credential_undecryptable"
        masked.append(item)
    return masked


async def get_sub_agent_config(session: AsyncSession) -> dict:
    """Registry sub-agent cho UI/INITIALIZATION; credential luôn bị che."""
    from sag_api.core.sub_agent_providers import sub_agent_provider_catalog

    row = await _load_row(session, _SUB_AGENTS_KEY)
    return {
        "providers": sub_agent_provider_catalog(),
        "entries": _masked_sub_agent_entries(_sub_agent_entries(row)),
    }


async def portable_sub_agent_config_with_secrets(session: AsyncSession) -> dict:
    """Registry đầy đủ để mã hoá portable; plaintext chỉ sống trong API process."""
    row = await _load_row(session, _SUB_AGENTS_KEY)
    entries: list[dict] = []
    for raw in _sub_agent_entries(row):
        item = dict(raw)
        stored = item.get("credential")
        if isinstance(stored, str) and stored:
            credential = decrypt_secret(stored, _settings.secret_key)
            if credential is None:
                provider = item.get("provider")
                raise ConfigurationError(
                    f"Không giải mã được credential của {provider}",
                    code="credential_undecryptable",
                )
            item["credential"] = credential
        entries.append(item)
    return {"entries": entries}


async def stored_sub_agent_credential(session: AsyncSession, provider: str) -> str | None:
    """Giải mã credential đúng slot để discovery model; không nhận endpoint từ client."""
    row = await _load_row(session, _SUB_AGENTS_KEY)
    for entry in _sub_agent_entries(row):
        if entry.get("provider") != provider:
            continue
        stored = entry.get("credential")
        if not isinstance(stored, str) or not stored:
            return None
        return decrypt_secret(stored, _settings.secret_key)
    return None


async def load_sub_agent_for_execution(
    session: AsyncSession,
    provider: str,
) -> dict | None:
    """Nạp một slot đã bật cho tầng thực thi nội bộ.

    Hàm này là ranh giới duy nhất đưa plaintext credential ra khỏi settings service.
    Giá trị chỉ sống trong process Brain; API và MCP không bao giờ trả dict này cho client.
    """
    row = await _load_row(session, _SUB_AGENTS_KEY)
    for entry in _sub_agent_entries(row):
        if entry.get("provider") != provider or not entry.get("enabled"):
            continue
        item = {key: value for key, value in entry.items() if key != "credential"}
        stored = entry.get("credential")
        item["credential"] = (
            decrypt_secret(stored, _settings.secret_key)
            if isinstance(stored, str) and stored
            else None
        )
        return item
    return None


async def save_sub_agent_config(
    session: AsyncSession,
    entries: list[dict],
    *,
    discovered_models: dict[str, set[str]] | None = None,
) -> dict:
    """Mã hoá credential và thay toàn bộ registry sub-agent.

    Credential rỗng giữ bản cũ theo provider. Riêng custom provider đổi endpoint thì
    bắt buộc nhập credential mới, tránh vô tình tái dùng bí mật cho một host khác.
    """
    row = await _load_row(session, _SUB_AGENTS_KEY)
    previous = {entry.get("provider"): entry for entry in _sub_agent_entries(row)}
    verified = discovered_models or {}
    prepared: list[dict] = []
    for entry in entries:
        item = dict(entry)
        provider = str(item.get("provider") or "")
        submitted = str(item.get("credential") or "").strip()
        if submitted:
            item["credential"] = encrypt_secret(submitted, _settings.secret_key)
        else:
            old = previous.get(provider, {})
            same_endpoint = _same_endpoint(old.get("base_url"), item.get("base_url"))
            if old.get("credential") and (
                provider != "custom" or same_endpoint
            ):
                item["credential"] = old["credential"]
            else:
                item.pop("credential", None)
        old = previous.get(provider, {})
        if provider == "custom":
            item.pop("model_verified", None)
        elif provider in verified:
            item["model_verified"] = item.get("model") in verified[provider]
        else:
            credential_unchanged = not submitted and item.get("credential") == old.get("credential")
            model_unchanged = item.get("model") == old.get("model")
            item["model_verified"] = bool(
                old.get("model_verified") and credential_unchanged and model_unchanged
            )
        prepared.append(item)

    value = {"entries": prepared}
    if row is None:
        session.add(Setting(scope=_SCOPE, key=_SUB_AGENTS_KEY, value=value))
    else:
        row.value = value
    await session.commit()
    return await get_sub_agent_config(session)


async def save_model_config(session: AsyncSession, patch: dict) -> dict:
    """Merge and save the model configuration: persist it and override the settings singleton; returns the effective configuration (masked).

    Rules (paired with `exclude_unset`):
    - field absent -> unchanged;
    - secret field with an empty value -> ignored (the existing secret is kept, so it cannot be wiped by accident); only an explicit non-empty value overrides it;
    - nullable field (base_url / dimensions) with an empty value -> the stored override is **removed**.

    Xoá override phải trả field về giá trị của môi trường, không phải ghi đè nó bằng `None`.
    Ghi `None` là biến "tôi không muốn ghi đè nữa" thành "ép rỗng, kể cả khi `.env` có giá trị" —
    và khi đó không còn đường nào từ UI quay về mặc định của bản triển khai.
    """
    row = await _load_row(session)
    raw = dict(row.value) if row and isinstance(row.value, dict) else {}
    stored = _normalize_overrides(raw)
    previous_entries = _provider_entries(stored)

    for key, value in patch.items():
        if key not in _FIELDS:
            continue
        if key == "llm_providers":
            # Danh sách gửi lên **thay thế toàn bộ** (xoá entry = không gửi entry đó nữa).
            # Key rỗng trong entry = giữ key cũ theo id, xem _encrypt_entries.
            stored["llm_providers"] = _encrypt_entries(
                [dict(entry) for entry in (value or [])],
                previous_entries,
            )
            continue
        if key in _SECRET_FIELDS:
            if value:  # only non-empty updates; empty/None keeps the previous value
                stored[key] = encrypt_secret(str(value), _settings.secret_key)
            continue
        if key in _NULLABLE_FIELDS and (value is None or value == ""):
            # Gỡ hẳn khoá: field quay về giá trị của môi trường thay vì bị ép None.
            stored.pop(key, None)
            continue
        stored[key] = value

    stored = _normalize_overrides(stored)

    if row is None:
        session.add(Setting(scope=_SCOPE, key=_KEY, value=stored))
    else:
        row.value = stored
    await session.commit()

    apply_overrides(_settings, stored)
    return effective_model_config()
