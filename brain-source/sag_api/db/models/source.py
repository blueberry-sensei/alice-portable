from __future__ import annotations

from sqlalchemy import JSON, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from sag_api.db.base import Base, IDMixin, TimestampMixin
from sag_api.enums import ConnectorKind, SourceStatus, SourceType


class Source(IDMixin, TimestampMixin, Base):
    """Source - maps one-to-one to an alicecore data source (source_config_id)."""

    __tablename__ = "sources"

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    source_type: Mapped[SourceType] = mapped_column(
        SAEnum(SourceType, native_enum=False, length=16), default=SourceType.DOCUMENT
    )
    connector_kind: Mapped[ConnectorKind] = mapped_column(
        SAEnum(ConnectorKind, native_enum=False, length=32),
        default=ConnectorKind.FILE_UPLOAD,
    )
    # alicecore data source identifier (one instance per source)
    sag_source_config_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Connector config + engine overrides (such as per-source language / entity_types)
    config: Mapped[dict] = mapped_column("config_json", JSON, default=dict)
    status: Mapped[SourceStatus] = mapped_column(
        SAEnum(SourceStatus, native_enum=False, length=16), default=SourceStatus.ACTIVE
    )
    document_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    event_count: Mapped[int] = mapped_column(Integer, default=0)
