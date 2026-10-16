from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field


class Diagnosis(BaseModel):
    failure_type: str
    hypotheses: list[str]
    evidence: list[str]


class VerificationResult(BaseModel):
    healthy: bool
    failure_type: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


@dataclass(slots=True)
class EpisodeAction:
    action_key: str
    reward: float
    duration_seconds: float
    success: bool
    metadata: dict[str, str] = field(default_factory=dict)
