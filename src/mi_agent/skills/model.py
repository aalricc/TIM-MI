from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class SkillStep(BaseModel):
    primitive_id: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class SkillStats(BaseModel):
    success_count: int = 0
    failure_count: int = 0
    avg_recovery_seconds: float = 0.0


class SkillRecord(BaseModel):
    name: str
    version: int = 1
    description: str
    failure_type: str
    trigger_fingerprint: dict[str, str] = Field(default_factory=dict)
    preconditions: list[str] = Field(default_factory=list)
    steps: list[SkillStep]
    expected_postcondition: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    active: bool = True
    stats: SkillStats = Field(default_factory=SkillStats)
