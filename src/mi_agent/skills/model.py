from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class SkillStep(BaseModel):
    primitive_id: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    observation_bindings: dict[str, str] = Field(default_factory=dict)

    def resolve_parameters(self, observations: dict[str, str]) -> dict[str, Any]:
        resolved = dict(self.parameters)
        for parameter_name, observation_key in self.observation_bindings.items():
            if observation_key in observations:
                resolved[parameter_name] = observations[observation_key]
        return resolved


class SkillStats(BaseModel):
    success_count: int = 0
    failure_count: int = 0
    avg_recovery_seconds: float = 0.0
    consecutive_failures: int = 0


class SkillProvenance(BaseModel):
    source: Literal["agent_verified_success", "imported_checkpoint"] = "agent_verified_success"
    source_incidents: list[str] = Field(default_factory=list)
    environment_fingerprint: dict[str, str] = Field(default_factory=dict)


class SkillRecord(BaseModel):
    schema_version: str = "v2"
    name: str
    version: int = 1
    description: str
    failure_type: str
    trigger_fingerprint: dict[str, str] = Field(default_factory=dict)
    preconditions: list[str] = Field(default_factory=list)
    steps: list[SkillStep]
    expected_postcondition: str
    provenance: SkillProvenance = Field(default_factory=SkillProvenance)
    lifecycle_state: Literal["candidate", "canary", "trusted", "deprecated"] = "candidate"
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    active: bool = True
    stats: SkillStats = Field(default_factory=SkillStats)
