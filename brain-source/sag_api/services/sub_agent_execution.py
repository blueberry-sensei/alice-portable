"""Gọi một model sub-agent đã đăng ký mà không làm lộ credential khỏi Brain."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.errors import (
    ConfigurationError,
    ServiceUnavailableError,
    UpstreamError,
    ValidationError,
)
from sag_api.core.sub_agent_providers import SUB_AGENT_PROVIDERS, get_sub_agent_provider
from sag_api.core.telemetry import LLMCallRecord, emit_llm_call
from sag_api.services import settings_service

_TIMEOUT_SECONDS = 90.0
_MAX_TASK_CHARS = 8_000
_MAX_CONTEXT_CHARS = 24_000
_MAX_RESULT_CHARS = 24_000
_MAX_OUTPUT_TOKENS = 2_048
_OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_OPENCODE_BASE_URLS = {
    "opencode-go": "https://opencode.ai/zen/go/v1",
    "opencode-zen": "https://opencode.ai/zen/v1",
}
_OPENCODE_MODEL_PREFIXES = {
    "opencode-go": "opencode-go/",
    "opencode-zen": "opencode/",
}


@dataclass(frozen=True, slots=True)
class SubAgentResult:
    provider: str
    display_name: str
    model: str
    content: str
    input_tokens: int
    output_tokens: int
    total_tokens: int


def _positive_int(value: object) -> int:
    return int(value) if isinstance(value, int) and value > 0 else 0


def _require_custom_base_url(value: object) -> str:
    raw = str(value or "").strip().rstrip("/")
    parsed = urlsplit(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError(
            "Custom provider cần base URL HTTP(S) hợp lệ đã lưu trong Settings",
            code="sub_agent_base_url_invalid",
        )
    return raw


def _prompt_messages(task: str, context: str) -> list[dict[str, str]]:
    system = (
        "You are a bounded coding analysis sub-agent. Work only from the task and context "
        "provided. Do not claim that you read or changed files, ran commands, or used tools. "
        "Return concrete findings, risks, and a proposed patch or next action."
    )
    user = task
    if context:
        user = f"{task}\n\nCONTEXT PROVIDED BY THE ORCHESTRATOR:\n{context}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _openai_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise UpstreamError(
            "Sub-agent không trả choices hợp lệ",
            code="sub_agent_response_invalid",
        )
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        text = "\n".join(
            str(item.get("text") or "") for item in content if isinstance(item, dict) and item.get("type") == "text"
        ).strip()
        if text:
            return text
    raise UpstreamError(
        "Sub-agent không trả nội dung text",
        code="sub_agent_response_invalid",
    )


def _anthropic_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        raise UpstreamError(
            "Claude không trả content hợp lệ",
            code="sub_agent_response_invalid",
        )
    text = "\n".join(
        str(item.get("text") or "") for item in content if isinstance(item, dict) and item.get("type") == "text"
    ).strip()
    if not text:
        raise UpstreamError(
            "Claude không trả nội dung text",
            code="sub_agent_response_invalid",
        )
    return text


def _gemini_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        raise UpstreamError(
            "Gemini không trả candidates hợp lệ",
            code="sub_agent_response_invalid",
        )
    content = candidates[0].get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        raise UpstreamError(
            "Gemini không trả content hợp lệ",
            code="sub_agent_response_invalid",
        )
    text = "\n".join(str(item.get("text") or "") for item in parts if isinstance(item, dict)).strip()
    if not text:
        raise UpstreamError(
            "Gemini không trả nội dung text",
            code="sub_agent_response_invalid",
        )
    return text


def _usage(payload: dict[str, Any], provider: str) -> tuple[int, int, int]:
    if provider == "gemini-cli":
        usage = payload.get("usageMetadata")
        if not isinstance(usage, dict):
            return 0, 0, 0
        input_tokens = _positive_int(usage.get("promptTokenCount"))
        output_tokens = _positive_int(usage.get("candidatesTokenCount"))
        total_tokens = _positive_int(usage.get("totalTokenCount"))
    else:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return 0, 0, 0
        input_tokens = _positive_int(usage.get("input_tokens", usage.get("prompt_tokens")))
        output_tokens = _positive_int(usage.get("output_tokens", usage.get("completion_tokens")))
        total_tokens = _positive_int(usage.get("total_tokens"))
    return input_tokens, output_tokens, total_tokens or input_tokens + output_tokens


def _short_error(response: httpx.Response) -> str:
    return " ".join((response.text or "").split())[:500]


def _raise_for_response(response: httpx.Response, display_name: str) -> None:
    if 200 <= response.status_code < 300:
        return
    detail = _short_error(response)
    if response.status_code in {401, 403}:
        raise ValidationError(
            f"Credential của {display_name} không hợp lệ hoặc không có quyền",
            code="sub_agent_credential_invalid",
        )
    if response.status_code == 429:
        raise ServiceUnavailableError(
            f"{display_name} đang giới hạn request",
            code="sub_agent_provider_rate_limited",
        )
    raise UpstreamError(
        f"{display_name} trả HTTP {response.status_code}" + (f": {detail}" if detail else ""),
        code="sub_agent_execution_failed",
    )


def _request_spec(
    entry: dict,
    credential: str,
    messages: list[dict[str, str]],
) -> tuple[str, dict[str, str], dict[str, Any], str]:
    provider = str(entry["provider"])
    model = str(entry["model"])
    if provider == "claude":
        return (
            _ANTHROPIC_URL,
            {
                "x-api-key": credential,
                "anthropic-version": "2023-06-01",
            },
            {
                "model": model,
                "max_tokens": _MAX_OUTPUT_TOKENS,
                "system": messages[0]["content"],
                "messages": messages[1:],
            },
            "https://api.anthropic.com/v1",
        )
    if provider == "gemini-cli":
        prompt = "\n\n".join(message["content"] for message in messages)
        endpoint = f"{_GEMINI_BASE_URL}/models/{quote(model, safe='')}:generateContent"
        return (
            endpoint,
            {"x-goog-api-key": credential},
            {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": _MAX_OUTPUT_TOKENS},
            },
            _GEMINI_BASE_URL,
        )
    if provider in _OPENCODE_BASE_URLS:
        prefix = _OPENCODE_MODEL_PREFIXES[provider]
        routed_model = model.removeprefix(prefix)
        base_url = _OPENCODE_BASE_URLS[provider]
        return (
            f"{base_url}/chat/completions",
            {"Authorization": f"Bearer {credential}"},
            {
                "model": routed_model,
                "messages": messages,
                "max_tokens": _MAX_OUTPUT_TOKENS,
            },
            base_url,
        )
    base_url = _require_custom_base_url(entry.get("base_url")) if provider == "custom" else "https://api.openai.com/v1"
    return (
        f"{base_url}/chat/completions" if provider == "custom" else _OPENAI_URL,
        {"Authorization": f"Bearer {credential}"},
        {
            "model": model,
            "messages": messages,
            "max_tokens": _MAX_OUTPUT_TOKENS,
        },
        base_url,
    )


async def list_available_sub_agents(session: AsyncSession) -> list[dict]:
    """Trả trạng thái gọi được đã che credential cho MCP."""
    config = await settings_service.get_sub_agent_config(session)
    available: list[dict] = []
    for entry in config["entries"]:
        if not entry.get("enabled"):
            continue
        provider = str(entry.get("provider") or "")
        verified = provider == "custom" or bool(entry.get("model_verified"))
        callable_now = bool(
            entry.get("model")
            and entry.get("credential_set")
            and verified
            and (provider != "custom" or entry.get("base_url"))
        )
        available.append(
            {
                "provider": provider,
                "display_name": get_sub_agent_provider(provider).display_name,
                "provider_name": entry.get("provider_name") or "",
                "model": entry.get("model") or "",
                "credential_set": bool(entry.get("credential_set")),
                "model_verified": entry.get("model_verified"),
                "callable": callable_now,
                "error": entry.get("error"),
            }
        )
    return available


async def invoke_sub_agent(
    session: AsyncSession,
    provider: str,
    task: str,
    *,
    context: str = "",
    actor: str = "unknown",
    transport: httpx.AsyncBaseTransport | None = None,
) -> SubAgentResult:
    """Gọi đúng slot đã lưu; endpoint và credential không bao giờ nhận từ MCP client."""
    if provider not in SUB_AGENT_PROVIDERS:
        raise ConfigurationError(
            f"Không có slot sub-agent: {provider}",
            code="sub_agent_not_configured",
        )
    normalized_task = (task or "").strip()[:_MAX_TASK_CHARS]
    normalized_context = (context or "").strip()[:_MAX_CONTEXT_CHARS]
    if not normalized_task:
        raise ValidationError("Task sub-agent không được để trống")

    entry = await settings_service.load_sub_agent_for_execution(session, provider)
    if entry is None:
        raise ConfigurationError(
            f"Slot {provider} chưa bật trong Settings → Sub Agents",
            code="sub_agent_not_enabled",
        )
    credential = str(entry.get("credential") or "")
    if not credential:
        raise ConfigurationError(
            f"Slot {provider} chưa có credential giải mã được",
            code="sub_agent_credential_required",
        )
    if provider != "custom" and not entry.get("model_verified"):
        raise ConfigurationError(
            f"Model của slot {provider} chưa được xác thực",
            code="sub_agent_model_unverified",
        )

    display_name = (
        str(entry.get("provider_name") or "").strip()
        or get_sub_agent_provider(provider).display_name
    )
    messages = _prompt_messages(normalized_task, normalized_context)
    endpoint, headers, body, api_base = _request_spec(entry, credential, messages)
    started = time.perf_counter()
    response: httpx.Response | None = None
    error: Exception | None = None
    input_tokens = output_tokens = total_tokens = 0
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT_SECONDS,
            follow_redirects=False,
            transport=transport,
        ) as client:
            response = await client.post(endpoint, headers=headers, json=body)
        _raise_for_response(response, display_name)
        try:
            payload = response.json()
        except ValueError as parse_error:
            raise UpstreamError(
                f"{display_name} trả JSON không hợp lệ",
                code="sub_agent_response_invalid",
            ) from parse_error
        if not isinstance(payload, dict):
            raise UpstreamError(
                f"{display_name} trả response không đúng contract",
                code="sub_agent_response_invalid",
            )
        if provider == "claude":
            content = _anthropic_text(payload)
        elif provider == "gemini-cli":
            content = _gemini_text(payload)
        else:
            content = _openai_text(payload)
        input_tokens, output_tokens, total_tokens = _usage(payload, provider)
        return SubAgentResult(
            provider=provider,
            display_name=display_name,
            model=str(entry["model"]),
            content=content[:_MAX_RESULT_CHARS],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )
    except (ValidationError, ConfigurationError, UpstreamError, ServiceUnavailableError) as caught:
        error = caught
        raise
    except httpx.TimeoutException as caught:
        error = caught
        raise ServiceUnavailableError(
            f"{display_name} không phản hồi kịp",
            code="sub_agent_provider_timeout",
        ) from caught
    except httpx.RequestError as caught:
        error = caught
        raise ServiceUnavailableError(
            f"Không kết nối được tới {display_name}",
            code="sub_agent_provider_unavailable",
        ) from caught
    except Exception as caught:
        error = caught
        raise
    finally:
        latency_ms = int((time.perf_counter() - started) * 1000)
        await emit_llm_call(
            LLMCallRecord(
                stage="generation",
                call_type="sub_agent",
                provider=provider,
                model=str(entry["model"]),
                api_base=api_base,
                ok=error is None and response is not None and response.is_success,
                failure_kind=(
                    "rate_limit"
                    if isinstance(error, ServiceUnavailableError)
                    and getattr(error, "code", "") == "sub_agent_provider_rate_limited"
                    else "upstream"
                    if error is not None
                    else None
                ),
                error=str(error)[:2000] if error is not None else None,
                latency_ms=latency_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                cost_usd=None,
                cost_source="unknown",
                actor=actor,
            )
        )
