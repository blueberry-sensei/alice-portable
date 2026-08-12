"""
Data model base classes

Base class of every Pydantic model
"""

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


class pipelineBaseModel(BaseModel):
    """Base pipeline model"""

    model_config = ConfigDict(
        # Allow ORM mode (construct from a SQLAlchemy object)
        from_attributes=True,
        # Use the enum value rather than the enum object
        use_enum_values=True,
        # Validate on assignment
        validate_assignment=True,
        # Fill in defaults for None
        populate_by_name=True,
    )


class TimestampMixin(BaseModel):
    """Timestamp mixin"""

    created_time: datetime = Field(default_factory=datetime.now, description="created at")
    updated_time: Optional[datetime] = Field(default=None, description="updated at")

    model_config = ConfigDict(from_attributes=True)


class MetadataMixin(BaseModel):
    """Extra data mixin"""

    extra_data: Optional[Dict[str, Any]] = Field(default=None, description="extra data (JSON)")

    model_config = ConfigDict(from_attributes=True)
