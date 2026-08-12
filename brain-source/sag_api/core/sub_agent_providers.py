"""Metadata provider dành cho sub-agent cấu hình trên giao diện.

Model của năm provider mặc định không nằm trong source. Chúng chỉ được lấy từ API
thật sau khi credential đã được provider chấp nhận. Riêng ``custom`` vẫn nhập model
thủ công vì Brain không được phép đoán contract của một endpoint tuỳ biến.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Literal

SubAgentProviderId = Literal[
    "claude",
    "codex",
    "opencode-go",
    "opencode-zen",
    "gemini-cli",
    "custom",
]

DiscoverableSubAgentProviderId = Literal[
    "claude",
    "codex",
    "opencode-go",
    "opencode-zen",
    "gemini-cli",
]


@dataclass(frozen=True, slots=True)
class SubAgentProviderSpec:
    id: SubAgentProviderId
    display_name: str
    credential_label: str
    credential_placeholder: str
    model_discovery: bool = True
    custom_model: bool = False
    base_url_configurable: bool = False

    def to_public_dict(self) -> dict[str, object]:
        return asdict(self)


_PROVIDER_SPECS = (
    SubAgentProviderSpec(
        id="claude",
        display_name="Claude",
        credential_label="Anthropic API key",
        credential_placeholder="sk-ant-…",
    ),
    SubAgentProviderSpec(
        id="codex",
        display_name="Codex",
        credential_label="OpenAI API key",
        credential_placeholder="sk-…",
    ),
    SubAgentProviderSpec(
        id="opencode-go",
        display_name="OpenCode GO",
        credential_label="OpenCode GO API key",
        credential_placeholder="API key",
    ),
    SubAgentProviderSpec(
        id="opencode-zen",
        display_name="OpenCode ZEN",
        credential_label="OpenCode ZEN API key",
        credential_placeholder="API key",
    ),
    SubAgentProviderSpec(
        id="gemini-cli",
        display_name="Gemini CLI",
        credential_label="Google AI API key",
        credential_placeholder="AIza…",
    ),
    SubAgentProviderSpec(
        id="custom",
        display_name="Custom provider",
        credential_label="Credential",
        credential_placeholder="Token / API key",
        model_discovery=False,
        custom_model=True,
        base_url_configurable=True,
    ),
)

SUB_AGENT_PROVIDERS = MappingProxyType({spec.id: spec for spec in _PROVIDER_SPECS})


def get_sub_agent_provider(provider: SubAgentProviderId) -> SubAgentProviderSpec:
    return SUB_AGENT_PROVIDERS[provider]


def sub_agent_provider_catalog() -> list[dict[str, object]]:
    return [spec.to_public_dict() for spec in _PROVIDER_SPECS]
