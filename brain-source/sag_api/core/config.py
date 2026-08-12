"""Application settings (pydantic-settings).

Every setting can be overridden by a `SAG_*` environment variable or by `.env`. Three backend groups:

- **sag metadata database** (users / sources / documents / threads): `database_url`
- **alicecore storage** (chunks / vectors / event graph): `sag_*` + `data_dir`
- **LLM / embedding** (extraction and answer generation): `llm_*` / `embedding_*`
- **Parse tài liệu** (PDF / Office -> Markdown, cục bộ): `document_parser`

Zero dependencies by default: SQLite metadata + alicecore local LanceDB. Production can switch the whole stack to Postgres.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from sag_api.core.model_providers import ModelProviderId, get_model_provider
from sag_api.enums import SearchStrategy, normalize_search_strategy

_DEFAULT_LLM_PROVIDER = get_model_provider("openai")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SAG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Application ─────────────────────────────────────────────────────
    app_name: str = "sag"
    environment: Literal["dev", "prod"] = "dev"
    debug: bool = True
    secret_key: str = "dev-insecure-secret-change-me-in-production-0123456789"
    access_token_expire_minutes: int = 60 * 24 * 7  # 7 days
    # Múi giờ hiển thị; web lưu múi giờ máy người dùng ở lần mở đầu tiên.
    # Nếu trình duyệt không xác định được thì giữ UTC. DB và API luôn dùng UTC.
    timezone: str = "UTC"
    # NoDecode lets comma-separated values reach the validator below, so the settings source does not force JSON decoding.
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:3000"])
    # When off, only the first user may register (deployment bootstrap); everyone else gets 403
    allow_registration: bool = True

    # ── sag metadata database ──────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./.data/sag.db"

    # ── Storage ─────────────────────────────────────────────────────────
    data_dir: str = "./.data/engine"  # alicecore data_dir（LanceDB + SQLite）
    # Log ghi ra file trong thư mục này (ngoài stdout). Trong stack, đây là bind-mount
    # nên xem được từ host mà không cần `docker compose logs`.
    log_dir: str = "./.data/logs"
    log_file_max_mb: int = Field(default=20, ge=1, le=500)
    log_file_backups: int = Field(default=5, ge=0, le=50)
    upload_dir: str = "./.data/uploads"  # where uploaded raw files land
    max_upload_mb: int = 25  # per-file upload limit
    job_concurrency: int = 2  # background processing concurrency
    document_extract_concurrency: int = Field(default=5, ge=1, le=50)  # per-document chunk extraction concurrency
    document_chunk_max_tokens: int = Field(default=1_000, ge=100, le=100_000)
    document_chunk_mode: Literal["standard", "heading_strict"] = "standard"
    # Uploaded documents already carry their own knowledge-oriented filter; the upstream strict filter on title/summary
    # is off by default so book bodies without a summary or title are not mistaken for noise.
    document_strict_filtering: bool = False
    job_max_attempts: int = 3  # max attempts for a retryable failure (first attempt included)
    engine_cache_size: int = 16  # engine-slot LRU cap (evicts the least recently used)
    engine_warmup_count: int = 4  # how many recently used source engines to warm up at startup
    # Allowed upload extensions (lowercase, dot included); an empty set means no restriction
    allowed_upload_exts: set[str] = {
        ".md",
        ".markdown",
        ".txt",
        ".text",
        ".pdf",
        ".docx",
        ".pptx",
        ".xls",
        ".xlsx",
        ".csv",
        ".tsv",
        ".html",
        ".htm",
        ".json",
        ".epub",
    }

    # ── Backend của alicecore ──────────────────────────────────────────
    # None → zero-infra (LanceDB + SQLite nội bộ, ghi vào data_dir)
    sag_vector_provider: Literal["lancedb", "es", "pgvector", "oceanbase"] = "lancedb"
    sag_relational_provider: Literal["sqlite", "postgres", "mysql", "oceanbase"] | None = None
    # Ngôn ngữ PROMPT trích xuất của alicecore (không phải ngôn ngữ giao diện).
    # Mặc định 'en' vì LLM bám JSON schema ổn định hơn; nội dung tiếng Việt vẫn bóc tốt.
    sag_language: Literal["en", "vi"] = "en"

    # A single production database (pgvector) reuses the same Postgres - assembled from these fields
    sag_pg_host: str = "localhost"
    sag_pg_port: int = 5432
    sag_pg_user: str = "sag"
    sag_pg_password: str = "sag"
    sag_pg_database: str = "sag"

    # ── LLM (answer generation + extraction) ────────────────────────────
    # Protocol, routing rules and technical defaults all live in the model_providers registry.
    llm_provider: ModelProviderId = _DEFAULT_LLM_PROVIDER.id
    llm_base_url: str | None = _DEFAULT_LLM_PROVIDER.default_base_url
    llm_api_key: str | None = None
    llm_model: str = _DEFAULT_LLM_PROVIDER.default_model
    llm_temperature: float = _DEFAULT_LLM_PROVIDER.default_temperature
    llm_max_tokens: int = 20_000
    llm_context_window: int = _DEFAULT_LLM_PROVIDER.default_context_window
    # 6 phút: model reasoning qua gateway chậm thường mất 2-3 phút cho một chunk; 60s là
    # cắt giữa chừng rồi báo timeout, không phải lỗi provider.
    llm_timeout_ms: int = Field(default=360_000, ge=1_000, le=600_000)
    llm_max_retries: int = Field(default=2, ge=0, le=10)
    # Extra request body passed through to chat/completions (JSON), e.g. {"enable_thinking": false};
    # when unset, qwen-family models get thinking disabled via LiteLLM reasoning_effort=none.
    llm_extra_body: dict | None = None

    # ── Chuỗi provider theo thứ tự ưu tiên ─────────────────────────────
    # Đây là **nguồn sự thật duy nhất** cho việc gọi LLM và chỉ được cấu hình qua UI
    # (Settings → Models, lưu bảng `settings`). Các trường `llm_*` phẳng phía trên chỉ còn là
    # ảnh chiếu của entry đầu chuỗi, giữ cho những chỗ đọc "đang dùng model nào" khỏi phải sửa.
    # Rỗng = CHƯA cấu hình → ingest/hỏi đáp từ chối chạy (không có key mặc định, không fallback).
    llm_providers: list[dict] = Field(default_factory=list)

    # ── Embedding (OpenAI-compatible; only the OpenAI provider can reuse the generation config) ───
    embedding_model: str = "bge-large-en-v1.5"
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
    embedding_dimensions: int | None = None
    # Embedding không đổi nhà khi lỗi (đổi model = đổi không gian vector, index sẽ lẫn hai hệ
    # toạ độ). Chỉ thử lại trên cùng endpoint; hết lượt là để document FAILED, không ghi thiếu vector.
    embedding_max_retries: int = Field(default=3, ge=0, le=10)
    # Hostname của container embedding do LAUNCHER dựng kèm (danh sách ngăn bằng dấu phẩy).
    # Chỉ dùng để trả lời một câu hỏi của launcher: "endpoint đang có hiệu lực có phải là
    # container tôi dựng không?" — nếu không thì container đó đang chạy không công và bị thu hồi.
    # Để trống (chạy ngoài stack ALICE) thì câu trả lời là "không biết" và không ai bị đụng tới.
    bundled_embedding_hosts: str = ""

    # ── Parse tài liệu (chuyển sang Markdown trước khi vào alicecore) ────
    # Chỉ còn MarkItDown chạy CỤC BỘ. Nhánh gọi dịch vụ parse của bên thứ ba
    # đã gỡ bỏ — tài liệu của người dùng không rời khỏi máy.
    document_parser: Literal["markitdown"] = "markitdown"

    # ── Retrieval defaults ──────────────────────────────────────────────
    search_strategy: SearchStrategy = "vector"
    search_top_k: int = 8
    # Whole-library retrieval first picks a bounded set of candidate sources; an explicit @ scope is capped the same way.
    search_source_candidate_limit: int = Field(default=16, ge=1, le=256)
    search_source_concurrency: int = Field(default=4, ge=1, le=32)
    # Precise mode (multi) includes a query-side LLM round trip; on timeout/failure/empty result it falls back to fast mode (vector).
    search_source_timeout: float = 12.0
    search_fallback_vector: bool = True

    # ── Knowledge universe ────────────────────────────────────────────────
    # The server hands down depth gates and scene budgets, so the frontend no longer scatters hard-coded thresholds.
    universe_manifest_source_limit: int = Field(default=256, ge=16, le=2048)
    universe_timeline_event_page_size: int = Field(default=20, ge=10, le=50)
    # The timeline returns only a one-screen factual projection of an event; the full neighbourhood loads through explicit paged exploration.
    universe_event_entity_limit: int = Field(default=8, ge=4, le=8)
    universe_lod_orbit_px: int = Field(default=72, ge=24, le=240)
    universe_lod_near_px: int = Field(default=180, ge=64, le=640)
    universe_lod_deep_px: int = Field(default=360, ge=120, le=1200)
    universe_lod_hysteresis_px: int = Field(default=24, ge=4, le=120)
    universe_lod_debounce_ms: int = Field(default=220, ge=50, le=2000)
    universe_proxy_budget_desktop: int = Field(default=15000, ge=256, le=16000)
    universe_proxy_budget_mobile: int = Field(default=4000, ge=128, le=4800)
    universe_node_budget_desktop: int = Field(default=700, ge=450, le=1200)
    universe_node_budget_mobile: int = Field(default=520, ge=450, le=800)
    universe_edge_budget_desktop: int = Field(default=1000, ge=600, le=1800)
    universe_edge_budget_mobile: int = Field(default=720, ge=600, le=1200)
    universe_planet_radius_min: float = Field(default=42.0, ge=12.0, le=160.0)
    universe_planet_radius_max: float = Field(default=132.0, ge=48.0, le=360.0)
    universe_planet_radius_scale: float = Field(default=22.0, ge=2.0, le=80.0)

    # ── Agent loop ──────────────────────────────────────────────────────
    agent_max_steps: int = 6  # max tool-call rounds (upper bound on multi-round retrieval)
    history_keep_recent: int = 8  # how many recent messages stay verbatim when history is compacted
    # Load only a bounded recent window; older conversation belongs in the rolling summary, not a full replay.
    history_load_limit: int = Field(default=200, ge=1, le=1000)

    # ── Telemetry ───────────────────────────────────────────────────────
    # Ghi lại mọi request LLM (token + chi phí) và mọi lần agent lấy tri thức qua MCP.
    # Dữ liệu nằm trong chính DB của brain, không gửi đi đâu.
    telemetry_enabled: bool = True
    telemetry_retention_days: int = Field(default=30, ge=1, le=365)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        """Allow the CORS origins to be configured as a comma-separated string."""
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("["):
                return json.loads(v)
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @field_validator("search_strategy", mode="before")
    @classmethod
    def _normalize_legacy_search_strategy(cls, value: object) -> object:
        # Backwards compatible with the pre-upgrade environment variable; the public API no longer accepts atomic.
        return normalize_search_strategy(value) if isinstance(value, str) else value

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        normalized = value.strip()
        try:
            ZoneInfo(normalized)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("timezone must be a valid IANA time zone") from error
        return normalized

    @property
    def llm_chain(self) -> list[dict]:
        """Chuỗi provider đang bật, đã sắp theo ưu tiên (nhỏ = ưu tiên cao)."""
        enabled = [
            entry
            for entry in self.llm_providers
            if entry.get("enabled", True) and entry.get("api_key") and entry.get("model")
        ]
        return sorted(enabled, key=lambda entry: entry.get("priority", 100))

    @property
    def llm_configured(self) -> bool:
        """Whether the LLM is configured (decides if extraction / Q&A can actually run)."""
        return bool(self.llm_chain)

    @property
    def routed_llm_model(self) -> str:
        """LiteLLM route name used by the whole call chain."""
        return get_model_provider(self.llm_provider).route_model(self.llm_model)

    @property
    def effective_llm_temperature(self) -> float:
        """Sampling capability constraints of the current provider."""
        return get_model_provider(self.llm_provider).resolve_temperature(self.llm_temperature)

    @property
    def effective_embedding_api_key(self) -> str | None:
        provider = get_model_provider(self.llm_provider)
        return self.embedding_api_key or (self.llm_api_key if provider.can_reuse_embedding_credentials else None)

    @property
    def effective_embedding_base_url(self) -> str | None:
        provider = get_model_provider(self.llm_provider)
        return self.embedding_base_url or (self.llm_base_url if provider.can_reuse_embedding_credentials else None)

    @property
    def effective_document_parser(self) -> Literal["markitdown"]:
        """Chỉ còn một parser chạy cục bộ; định tuyến theo định dạng nằm ở parsing/."""
        return "markitdown"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

#: Fields that really came from `SAG_*` / `.env` at import time, as opposed to falling back to a default.
#:
#: Ghi lại **trước** khi DB ghi đè singleton. Không có ảnh chụp này thì sau lúc khởi động không
#: còn cách nào phân biệt "giá trị này đến từ .env" với "giá trị này đến từ bảng settings" — và
#: đó chính là cách người dùng sửa `.env` rồi ngồi đợi phép màu: DB thắng, im lặng, không dấu vết.
ENV_PROVIDED_FIELDS: frozenset[str] = frozenset(settings.model_fields_set)

#: Giá trị của mọi field **trước khi** DB ghi đè — tức giá trị của môi trường, hoặc mặc định.
#:
#: Có bảng này thì `apply_overrides` mới dựng lại được trạng thái đầy đủ từ (môi trường + override
#: đang lưu). Không có nó, gỡ một override trong UI chỉ khiến DB thôi khai giá trị đó, còn singleton
#: vẫn giữ giá trị cũ tới lần restart — người dùng bấm xoá mà không thấy gì đổi.
ENV_BASELINE: dict[str, object] = {name: getattr(settings, name) for name in type(settings).model_fields}


def env_var_name(field: str) -> str:
    """Tên biến môi trường tương ứng một field của `Settings` (env_prefix = `SAG_`)."""
    return f"SAG_{field.upper()}"


def same_endpoint(left: str | None, right: str | None) -> bool:
    """Hai base_url có trỏ về cùng một nơi không (bỏ qua khác biệt vô nghĩa)."""
    return (left or "").strip().rstrip("/").casefold() == (right or "").strip().rstrip("/").casefold()
