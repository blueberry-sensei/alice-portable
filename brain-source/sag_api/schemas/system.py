from __future__ import annotations

from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator

from sag_api.core.model_providers import ModelProviderId
from sag_api.core.sub_agent_providers import (
    SUB_AGENT_PROVIDERS,
    DiscoverableSubAgentProviderId,
    SubAgentProviderId,
    get_sub_agent_provider,
)
from sag_api.enums import SearchStrategy


class SystemPreferencesUpdate(BaseModel):
    timezone: str = Field(min_length=1, max_length=100)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        normalized = value.strip()
        try:
            ZoneInfo(normalized)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("Phải dùng múi giờ IANA hợp lệ, ví dụ Asia/Ho_Chi_Minh") from error
        return normalized


PortableConfigKind = Literal["alice-model-config", "alice-sub-agent-config"]


class PortableConfigKdf(BaseModel):
    name: Literal["scrypt"]
    salt: str = Field(min_length=1, max_length=256)
    n: int
    r: int
    p: int


class PortableConfigBundle(BaseModel):
    format: Literal["alice-portable-config"]
    version: Literal[1]
    kind: PortableConfigKind
    contains_secrets: Literal[True]
    cipher: Literal["AES-256-GCM"]
    kdf: PortableConfigKdf
    nonce: str = Field(min_length=1, max_length=256)
    ciphertext: str = Field(min_length=1, max_length=2_000_000)


class ConfigTransferExportRequest(BaseModel):
    kind: PortableConfigKind
    passphrase: str = Field(min_length=12, max_length=256)


class ConfigTransferImportRequest(BaseModel):
    bundle: PortableConfigBundle
    passphrase: str = Field(min_length=12, max_length=256)


class SubAgentEntry(BaseModel):
    """Một slot sub-agent trên UI; credential rỗng nghĩa là giữ bản đã lưu."""

    provider: SubAgentProviderId
    model: str = Field(default="", max_length=200)
    credential: str | None = Field(default=None, max_length=4096)
    provider_name: str = Field(default="", max_length=100)
    base_url: str | None = Field(default=None, max_length=500)
    enabled: bool = False

    @field_validator("model", "provider_name")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("base_url", "credential")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class SubAgentConfigUpdate(BaseModel):
    """Danh sách xuất hiện thì thay toàn bộ sáu slot cấu hình."""

    entries: list[SubAgentEntry] = Field(max_length=len(SUB_AGENT_PROVIDERS))

    @field_validator("entries")
    @classmethod
    def validate_entries(cls, entries: list[SubAgentEntry]) -> list[SubAgentEntry]:
        seen: set[str] = set()
        for entry in entries:
            if entry.provider in seen:
                raise ValueError(f"provider sub-agent bị trùng: {entry.provider}")
            seen.add(entry.provider)
            spec = get_sub_agent_provider(entry.provider)
            if entry.enabled and not entry.model:
                raise ValueError(f"{spec.display_name} cần chọn model")
            if spec.custom_model and entry.enabled and not entry.provider_name:
                raise ValueError("Custom provider cần tên provider")
        return entries


class SubAgentModelDiscoveryRequest(BaseModel):
    """Lấy model live bằng key mới, hoặc để trống để dùng key đã mã hoá trong DB."""

    provider: DiscoverableSubAgentProviderId
    credential: str | None = Field(default=None, max_length=4096)

    @field_validator("credential")
    @classmethod
    def strip_discovery_credential(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class LLMProviderEntry(BaseModel):
    """Một provider trong chuỗi ưu tiên.

    `api_key` để trống nghĩa là **giữ key đã lưu** của entry cùng `id` — nhờ đó UI sửa được
    nhãn / thứ tự / model mà không phải nhập lại key (server không bao giờ gửi key ra ngoài).
    """

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    provider: ModelProviderId
    model: str = Field(min_length=1, max_length=200)
    label: str = Field(default="", max_length=100)
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    priority: int = Field(default=100, ge=1, le=999)
    enabled: bool = True
    #: Tham số riêng của gateway, ví dụ ép backend OpenRouter:
    #: {"provider": {"order": ["deepinfra/fp4"], "allow_fallbacks": false}}
    extra_body: dict | None = None
    #: Bị 429 thì tạm bỏ qua provider này bao lâu (giây).
    cooldown_seconds: float = Field(default=60.0, ge=0, le=3600)
    #: Ghi đè tham số hành vi ở mức entry (bỏ trống = dùng cấu hình chung bên dưới).
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=32768)
    timeout_ms: int | None = Field(default=None, ge=1_000, le=600_000)
    max_retries: int | None = Field(default=None, ge=0, le=10)


class ModelConfigUpdate(BaseModel):
    """Partial update of the model and knowledge-base configuration (fields that do not appear stay unchanged).

    An empty secret field means "keep the current value" (it is not cleared); an empty base_url / dimensions means clear it.
    When `llm_providers` appears it **replaces the whole** priority chain.
    """

    llm_providers: list[LLMProviderEntry] | None = Field(default=None, max_length=20)
    llm_temperature: float | None = Field(default=None, ge=0, le=2)
    llm_max_tokens: int | None = Field(default=None, ge=1, le=32768)
    llm_context_window: int | None = Field(default=None, ge=1024, le=2_000_000)
    llm_timeout_ms: int | None = Field(default=None, ge=1_000, le=600_000)
    llm_max_retries: int | None = Field(default=None, ge=0, le=10)

    embedding_model: str | None = Field(default=None, min_length=1, max_length=200)
    embedding_base_url: str | None = Field(default=None, max_length=500)
    embedding_api_key: str | None = Field(default=None, max_length=500)
    embedding_dimensions: int | None = Field(default=None, ge=1, le=8192)

    document_parser: Literal["markitdown"] | None = None
    document_extract_concurrency: int | None = Field(default=None, ge=1, le=50)
    document_chunk_max_tokens: int | None = Field(default=None, ge=100, le=100_000)
    document_chunk_mode: Literal["standard", "heading_strict"] | None = None

    search_strategy: SearchStrategy | None = None
    search_top_k: int | None = Field(default=None, ge=1, le=50)
    sag_language: Literal["en", "vi"] | None = None

    @field_validator("document_parser")
    @classmethod
    def reject_null_parser_fields(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("document_parser không được để null")
        return value

    @field_validator("document_extract_concurrency", "document_chunk_max_tokens")
    @classmethod
    def reject_null_document_numbers(cls, value: int | None) -> int:
        if value is None:
            raise ValueError("Knowledge-base parsing parameters cannot be null")
        return value

    @field_validator("document_chunk_mode")
    @classmethod
    def reject_null_chunk_mode(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("Chunking mode cannot be null")
        return value

    @field_validator("llm_timeout_ms", "llm_max_retries")
    @classmethod
    def reject_null_llm_resilience_fields(cls, value: int | None) -> int:
        if value is None:
            raise ValueError("Model timeout and retry count cannot be null")
        return value

    @field_validator("llm_providers")
    @classmethod
    def validate_provider_chain(cls, value: list[LLMProviderEntry] | None) -> list[LLMProviderEntry] | None:
        """`id` phải duy nhất — nó là khoá để giữ key cũ và để đối chiếu log lỗi."""
        if value is None:
            return value
        seen: set[str] = set()
        for entry in value:
            if entry.id in seen:
                raise ValueError(f"id của provider bị trùng: {entry.id}")
            seen.add(entry.id)
        return value
