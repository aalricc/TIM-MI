from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ActionKind(StrEnum):
    PRIMITIVE = "primitive"
    SKILL = "skill"


class ActionStep(BaseModel):
    kind: ActionKind
    action_id: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""


class PlanCandidate(BaseModel):
    name: str
    score_hint: float = 0.0
    steps: list[ActionStep]


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


@dataclass(slots=True)
class IncidentContext:
    incident_id: str
    failure_type: str
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    attempt: int = 0
    actions: list[EpisodeAction] = field(default_factory=list)
