"""Xác thực credential và lấy model sub-agent trực tiếp từ provider."""

from __future__ import annotations

from collections.abc import Iterable

import httpx

from sag_api.core.errors import ServiceUnavailableError, UpstreamError, ValidationError
from sag_api.core.sub_agent_providers import (
    DiscoverableSubAgentProviderId,
    get_sub_agent_provider,
)

_TIMEOUT_SECONDS = 20.0
_ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
_OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
_GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_OPENCODE_MODELS_URLS = {
    "opencode-go": "https://opencode.ai/zen/go/v1/models",
    "opencode-zen": "https://opencode.ai/zen/v1/models",
}
_OPENCODE_MODEL_PREFIXES = {
    "opencode-go": "opencode-go",
    "opencode-zen": "opencode",
}
# Probe chỉ xin đúng 1 token, nhưng vẫn là một lượt inference thật nên ưu tiên bậc rẻ trước.
_OPENCODE_CHEAP_HINTS = ("-free", "flash", "nano", "mini", "lite", "haiku")
_OPENCODE_PROBE_ATTEMPTS = 3


def _unique_strings(values: Iterable[object]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        normalized = value.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _raise_for_provider_response(response: httpx.Response, provider: str) -> None:
    if response.status_code in {401, 403}:
        # Đây là credential của provider ngoài, không phải SAG session. Trả 401 ở đây sẽ làm
        # web hiểu nhầm token đăng nhập Brain hết hạn và đăng xuất người dùng.
        raise ValidationError(
            f"API key của {provider} không hợp lệ hoặc không có quyền",
            code="sub_agent_credential_invalid",
        )
    if response.status_code == 429:
        raise ServiceUnavailableError(
            f"{provider} đang giới hạn request; hãy thử lại sau",
            code="sub_agent_provider_rate_limited",
        )
    if response.status_code >= 400:
        raise UpstreamError(
            f"{provider} trả HTTP {response.status_code} khi lấy model",
            code="sub_agent_model_discovery_failed",
        )


def _json_object(response: httpx.Response, provider: str) -> dict:
    try:
        payload = response.json()
    except ValueError as error:
        raise UpstreamError(
            f"{provider} trả dữ liệu model không hợp lệ",
            code="sub_agent_model_discovery_failed",
        ) from error
    if not isinstance(payload, dict):
        raise UpstreamError(
            f"{provider} trả dữ liệu model không đúng contract",
            code="sub_agent_model_discovery_failed",
        )
    return payload


async def _discover_anthropic(client: httpx.AsyncClient, credential: str) -> list[str]:
    headers = {
        "x-api-key": credential,
        "anthropic-version": "2023-06-01",
    }
    models: list[object] = []
    after_id: str | None = None
    for _ in range(10):
        params: dict[str, str | int] = {"limit": 1000}
        if after_id:
            params["after_id"] = after_id
        response = await client.get(_ANTHROPIC_MODELS_URL, headers=headers, params=params)
        _raise_for_provider_response(response, "Claude")
        payload = _json_object(response, "Claude")
        page = payload.get("data")
        if not isinstance(page, list):
            raise UpstreamError(
                "Claude không trả danh sách model",
                code="sub_agent_model_discovery_failed",
            )
        models.extend(item.get("id") for item in page if isinstance(item, dict))
        if not payload.get("has_more"):
            break
        after_id = payload.get("last_id") if isinstance(payload.get("last_id"), str) else None
        if not after_id:
            break
    return _unique_strings(models)


async def _discover_openai(client: httpx.AsyncClient, credential: str) -> list[str]:
    response = await client.get(
        _OPENAI_MODELS_URL,
        headers={"Authorization": f"Bearer {credential}"},
    )
    _raise_for_provider_response(response, "Codex")
    payload = _json_object(response, "Codex")
    data = payload.get("data")
    if not isinstance(data, list):
        raise UpstreamError(
            "OpenAI không trả danh sách model",
            code="sub_agent_model_discovery_failed",
        )
    return _unique_strings(item.get("id") for item in data if isinstance(item, dict))


async def _discover_gemini(client: httpx.AsyncClient, credential: str) -> list[str]:
    response = await client.get(
        _GEMINI_MODELS_URL,
        headers={"x-goog-api-key": credential},
        params={"pageSize": 1000},
    )
    _raise_for_provider_response(response, "Gemini CLI")
    payload = _json_object(response, "Gemini CLI")
    data = payload.get("models")
    if not isinstance(data, list):
        raise UpstreamError(
            "Google AI không trả danh sách model",
            code="sub_agent_model_discovery_failed",
        )
    # API công bố capability ngay trên từng model; chỉ model sinh nội dung mới dùng được cho CLI.
    return _unique_strings(
        str(item.get("name", "")).removeprefix("models/")
        for item in data
        if isinstance(item, dict)
        and "generateContent" in (item.get("supportedGenerationMethods") or [])
    )


def _short_detail(response: httpx.Response) -> str:
    """Trích lời của provider để báo lỗi thật, không phải lỗi do Brain đoán."""
    return " ".join((response.text or "").split())[:200]


def _opencode_probe_candidates(catalog: list[str]) -> list[str]:
    ranked = sorted(
        catalog,
        key=lambda model: next(
            (index for index, hint in enumerate(_OPENCODE_CHEAP_HINTS) if hint in model.lower()),
            len(_OPENCODE_CHEAP_HINTS),
        ),
    )
    return ranked[:_OPENCODE_PROBE_ATTEMPTS]


def _is_opencode_auth_error(response: httpx.Response) -> bool:
    """Phân biệt 401 AuthError (key sai) với 401 ModelError (model không nằm trong plan).

    Gateway OpenCode trả 401 cho cả hai trường hợp. Nếu không phân biệt, plan GO với key
    đúng vẫn bị đánh trượt chỉ vì probe chọn nhầm model ZEN không có trong plan.
    """
    try:
        body = response.json()
        error = body.get("error", {})
        if isinstance(error, dict):
            error_type = str(error.get("type", "")).lower()
            message = str(error.get("message", "")).lower()
            if "autherror" in error_type:
                return True
            if "invalid api key" in message or "missing api key" in message:
                return True
            if "modelerror" in error_type or "not supported" in message:
                return False
    except (ValueError, TypeError):
        pass
    text = (response.text or "").lower()
    if "autherror" in text or "invalid api key" in text or "missing api key" in text:
        return True
    return False


async def _verify_opencode_credential(
    client: httpx.AsyncClient,
    name: str,
    base_url: str,
    headers: dict[str, str],
    catalog: list[str],
) -> None:
    """Probe bằng một request inference HỢP LỆ; gateway OpenCode không có endpoint kiểm key.

    Đo trực tiếp trên gateway thật (2026-07-28)::

        body {}                        -> 401 ModelError "Model {{model}} is not supported"
        body {} + key sai              -> 401 ModelError  (y hệt)
        model hợp lệ, không key        -> 401 AuthError   "Missing API key."
        model hợp lệ, key sai          -> 401 AuthError   "Invalid API key."

    Nghĩa là 401 chỉ mang nghĩa "key sai" khi request vốn đã hợp lệ về mặt model. Bản trước
    probe bằng body rỗng nên nhận 401 trong **mọi** trường hợp — key đúng cũng bị đánh trượt.
    """
    last_status: int | None = None
    last_detail = ""
    for model in _opencode_probe_candidates(catalog):
        probe = await client.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json={
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            },
        )
        if probe.status_code == 200:
            return
        if probe.status_code in {401, 403}:
            if _is_opencode_auth_error(probe):
                raise ValidationError(
                    f"API key của {name} không hợp lệ hoặc không có quyền",
                    code="sub_agent_credential_invalid",
                )
            # ModelError: model này không nằm trong plan (vd GO không truy cập
            # được model ZEN). Không phải lỗi key — thử model tiếp theo.
            last_status = probe.status_code
            last_detail = _short_detail(probe)
            continue
        if probe.status_code == 429:
            raise ServiceUnavailableError(
                f"{name} đang giới hạn request; hãy thử lại sau",
                code="sub_agent_provider_rate_limited",
            )
        last_status = probe.status_code
        last_detail = _short_detail(probe)

    raise UpstreamError(
        f"{name} không xác nhận được API key: HTTP {last_status} · {last_detail or 'không có nội dung'}. "
        "Gateway này trả cùng một lỗi cho key sai lẫn lúc chính nó hỏng, nên hãy kiểm tra lại key "
        "rồi thử lại sau ít phút.",
        code="sub_agent_credential_check_failed",
    )


async def _discover_opencode(
    client: httpx.AsyncClient,
    provider: DiscoverableSubAgentProviderId,
    credential: str,
) -> list[str]:
    name = get_sub_agent_provider(provider).display_name
    models_url = _OPENCODE_MODELS_URLS[provider]
    base_url = models_url.removesuffix("/models")
    headers = {"Authorization": f"Bearer {credential}"}

    # /models của OpenCode là catalog công khai (curl không kèm key vẫn trả 200), nên tự nó
    # KHÔNG chứng minh key đúng. Lấy catalog trước chỉ để biết probe bằng model nào.
    response = await client.get(models_url, headers=headers)
    _raise_for_provider_response(response, name)
    payload = _json_object(response, name)
    data = payload.get("data")
    if not isinstance(data, list):
        raise UpstreamError(
            f"{name} không trả danh sách model",
            code="sub_agent_model_discovery_failed",
        )
    catalog = _unique_strings(
        item.get("id") for item in data if isinstance(item, dict) and isinstance(item.get("id"), str)
    )
    if not catalog:
        raise UpstreamError(
            f"{name} không trả model nào dùng được",
            code="sub_agent_models_empty",
        )

    await _verify_opencode_credential(client, name, base_url, headers, catalog)

    prefix = _OPENCODE_MODEL_PREFIXES[provider]
    return [f"{prefix}/{model}" for model in catalog]


async def discover_sub_agent_models(
    provider: DiscoverableSubAgentProviderId,
    credential: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[str]:
    """Trả model provider thực cho credential này; không cache và không log credential."""
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT_SECONDS,
            follow_redirects=False,
            transport=transport,
        ) as client:
            if provider == "claude":
                models = await _discover_anthropic(client, credential)
            elif provider == "codex":
                models = await _discover_openai(client, credential)
            elif provider == "gemini-cli":
                models = await _discover_gemini(client, credential)
            else:
                models = await _discover_opencode(client, provider, credential)
    except (ServiceUnavailableError, UpstreamError, ValidationError):
        raise
    except httpx.TimeoutException as error:
        raise ServiceUnavailableError(
            f"{provider} không phản hồi kịp khi lấy model",
            code="sub_agent_provider_timeout",
        ) from error
    except httpx.RequestError as error:
        raise ServiceUnavailableError(
            f"Không kết nối được tới {provider}",
            code="sub_agent_provider_unavailable",
        ) from error

    if not models:
        raise UpstreamError(
            f"{provider} không trả model nào dùng được",
            code="sub_agent_models_empty",
        )
    return models
