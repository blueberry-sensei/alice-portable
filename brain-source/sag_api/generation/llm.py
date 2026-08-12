"""Unified LLM client for generation, streaming, and Agent tool calls.

Every configured provider uses the same LiteLLM boundary. Provider-specific
model routing and capability rules live in ``core.model_providers``; this
adapter only translates the normalized response into sag_agent events.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterator
from typing import Any

from sag_agent import CancellationToken, ModelChunk, ModelRequest, Usage
from sag_agent import ToolCall as RuntimeToolCall
from sag_api.core.config import Settings
from sag_api.core.errors import ConfigurationError, UpstreamError
from sag_api.core.litellm_policy import apply_litellm_completion_policy
from sag_api.core.llm_routing import ChainRunner
from sag_api.core.logging import get_logger
from sag_api.core.model_providers import get_model_provider

log = get_logger("generation")

Message = dict[str, Any]


async def _litellm_completion(**kwargs: Any) -> Any:
    """Import lazily so an unconfigured server can still start without provider work."""
    from litellm import acompletion

    return await acompletion(**kwargs)


def _attr(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


class LLMClient:
    """Client sinh nội dung, có định tuyến nhiều provider theo thứ tự ưu tiên.

    Failover xảy ra ở **thời điểm gọi provider**. Với stream, nghĩa là: lỗi khi mở stream
    (429, sai key, model không tồn tại — phần lớn trường hợp) thì đổi nhà; còn nếu stream đã
    mở và vỡ giữa dòng thì báo lỗi, không ghép nửa câu của hai model vào nhau.
    """

    def __init__(self, settings: Settings, runner: ChainRunner | None = None) -> None:
        self._settings = settings
        self._runner = runner or ChainRunner()

    @property
    def configured(self) -> bool:
        return self._settings.llm_configured

    @property
    def runner(self) -> ChainRunner:
        return self._runner

    def _ensure_configured(self) -> None:
        if not self.configured:
            raise ConfigurationError(
                "Chưa cấu hình LLM. Mở Settings → Models và thêm ít nhất một provider (có API key)."
            )

    def health(self) -> list[dict[str, Any]]:
        """Tình trạng từng provider trong chuỗi (cho API/UI)."""
        return self._runner.health_snapshot(self._settings.llm_chain)

    def _request_for(
        self,
        entry: dict,
        messages: list[Message],
        *,
        stream: bool,
        tools: list[dict] | None,
        tool_choice: str | dict | None,
        response_format: dict | None = None,
    ) -> dict[str, Any]:
        spec = get_model_provider(entry.get("provider") or "openai")
        temperature = entry.get("temperature")
        timeout_ms = entry.get("timeout_ms") or self._settings.llm_timeout_ms
        request: dict[str, Any] = {
            "model": spec.route_model(str(entry.get("model") or "")),
            "api_key": entry.get("api_key"),
            "timeout": timeout_ms / 1000,
            # Retry do ChainRunner quản để mỗi lần thử đều vào log; tắt retry ngầm của LiteLLM.
            "num_retries": 0,
            "messages": messages,
            "temperature": spec.resolve_temperature(
                self._settings.llm_temperature if temperature is None else temperature
            ),
            "max_tokens": entry.get("max_tokens") or self._settings.llm_max_tokens,
            "stream": stream,
        }
        if tools:
            request["tools"] = tools
            if tool_choice is not None:
                request["tool_choice"] = tool_choice
        if response_format is not None:
            request["response_format"] = response_format
        if entry.get("base_url"):
            request["api_base"] = entry["base_url"]
        if entry.get("extra_body"):
            request["extra_body"] = dict(entry["extra_body"])
        return apply_litellm_completion_policy(self._settings, request)

    async def _create_completion(
        self,
        messages: list[Message],
        *,
        stream: bool = False,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        response_format: dict | None = None,
    ) -> Any:
        """Gọi provider đầu tiên còn khoẻ; 429/hết quota/sai key thì tự chuyển sang provider kế."""

        async def call(entry: dict) -> Any:
            return await _litellm_completion(
                **self._request_for(
                    entry, messages, stream=stream, tools=tools, tool_choice=tool_choice,
                    response_format=response_format,
                )
            )

        return await self._runner.run(self._settings.llm_chain, call, stage="generation")

    @staticmethod
    async def _close_stream(stream: Any) -> None:
        close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result

    async def stream_turn(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[ModelChunk]:
        """Stream one provider turn, including native function calls.

        A direct answer and a tool decision now share one provider request. This is
        the adapter required by sag_agent.ModelProvider.
        """

        self._ensure_configured()
        tool_parts: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        stream = None
        try:
            stream = await self._create_completion(
                [message.to_model_dict() for message in request.messages],
                tools=list(request.tools) or None,
                tool_choice=request.tool_choice if request.tools else None,
                stream=True,
            )
            async for chunk in stream:
                cancellation.raise_if_cancelled()
                raw_usage = _attr(chunk, "usage")
                if raw_usage is not None:
                    prompt_details = _attr(raw_usage, "prompt_tokens_details")
                    completion_details = _attr(raw_usage, "completion_tokens_details")
                    yield ModelChunk(
                        usage=Usage(
                            input_tokens=int(_attr(raw_usage, "prompt_tokens", 0) or 0),
                            output_tokens=int(_attr(raw_usage, "completion_tokens", 0) or 0),
                            cached_tokens=int(_attr(prompt_details, "cached_tokens", 0) or 0),
                            reasoning_tokens=int(_attr(completion_details, "reasoning_tokens", 0) or 0),
                        )
                    )
                choices = _attr(chunk, "choices", []) or []
                if not choices:
                    continue
                choice = choices[0]
                finish_reason = _attr(choice, "finish_reason") or finish_reason
                delta = _attr(choice, "delta", {})
                token = _attr(delta, "content")
                if token:
                    yield ModelChunk(text_delta=token)
                for fallback_index, tool_delta in enumerate(_attr(delta, "tool_calls") or []):
                    index = _attr(tool_delta, "index")
                    index = fallback_index if index is None else int(index)
                    part = tool_parts.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    tool_id = _attr(tool_delta, "id")
                    if tool_id:
                        part["id"] += str(tool_id)
                    function = _attr(tool_delta, "function")
                    if function is not None:
                        name = _attr(function, "name")
                        arguments = _attr(function, "arguments")
                        if name:
                            part["name"] += str(name)
                        if arguments:
                            part["arguments"] += (
                                arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
                            )

            calls: list[RuntimeToolCall] = []
            for index in sorted(tool_parts):
                part = tool_parts[index]
                raw_arguments = part["arguments"] or "{}"
                parse_error = None
                arguments: dict = {}
                try:
                    candidate = json.loads(raw_arguments)
                    if isinstance(candidate, dict):
                        arguments = candidate
                    else:
                        parse_error = "tool arguments must decode to an object"
                except (json.JSONDecodeError, TypeError) as exc:
                    parse_error = str(exc)
                calls.append(
                    RuntimeToolCall(
                        id=part["id"] or f"tool-{request.turn}-{index}",
                        name=part["name"],
                        arguments=arguments,
                        raw_arguments=raw_arguments,
                        parse_error=parse_error,
                    )
                )
            if calls or finish_reason:
                yield ModelChunk(tool_calls=tuple(calls), finish_reason=finish_reason)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("Streaming LLM round failed: %s", e)
            raise UpstreamError(f"Generation failed: {e}") from e
        finally:
            if stream is not None:
                try:
                    await self._close_stream(stream)
                except Exception as e:  # noqa: BLE001
                    log.debug("Failed to close the LLM round stream: %s", e)

    async def complete(
        self, messages: list[Message], *, response_format: dict | None = None
    ) -> str:
        """Sinh một lượt, không stream.

        `response_format` để nút Test thử ĐÚNG structured output mà đường trích xuất dùng.
        Gateway nhận chat thường nhưng từ chối `json_schema` là chuyện có thật; Test không
        thử thì nó báo xanh rồi ingest mới vỡ — đúng thứ vừa xảy ra.
        """
        self._ensure_configured()
        try:
            resp = await self._create_completion(messages, response_format=response_format)
            choices = _attr(resp, "choices", []) or []
            if not choices:
                raise UpstreamError("The model returned no candidate answer")
            return _attr(_attr(choices[0], "message", {}), "content", "") or ""
        except Exception as e:  # noqa: BLE001
            raise UpstreamError(f"Generation failed: {e}") from e

    async def stream_complete(self, messages: list[Message]) -> AsyncIterator[str]:
        """Stream plain text completion deltas without the Agent/tool protocol."""

        self._ensure_configured()
        stream = None
        try:
            stream = await self._create_completion(messages, stream=True)
            async for chunk in stream:
                choices = _attr(chunk, "choices", []) or []
                if not choices:
                    continue
                token = _attr(_attr(choices[0], "delta", {}), "content")
                if token:
                    yield token
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            raise UpstreamError(f"Generation failed: {e}") from e
        finally:
            # Closing explicitly makes browser aborts release the upstream HTTP
            # connection immediately, even when the stream is only partly read.
            if stream is not None:
                try:
                    await self._close_stream(stream)
                except Exception as e:  # noqa: BLE001
                    log.debug("Failed to close the LLM stream: %s", e)
