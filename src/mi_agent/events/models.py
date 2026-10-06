from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class Event(BaseModel):
    event_type: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    payload: dict[str, Any] = Field(default_factory=dict)
