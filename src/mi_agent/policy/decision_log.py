from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field, field_validator


class RewardComponents(BaseModel):
    verifier_success: float = 0.0
    duration_penalty: float = 0.0
    attempt_penalty: float = 0.0
    rollback_penalty: float = 0.0
    last_resort_penalty: float = 0.0
    terminal_penalty: float = 0.0
    net_reward: float


class DecisionLogEntry(BaseModel):
    schema_version: str = "v1"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    incident_id: str
    failure_type: str
    state_features: dict[str, str]
    candidate_set: list[str]
    chosen_action: str
    propensities: dict[str, float]
    policy_version: str
    verifier_healthy: bool | None = None
    reward_components: RewardComponents

    @field_validator("propensities")
    @classmethod
    def _validate_propensities(cls, value: dict[str, float]) -> dict[str, float]:
        if not value:
            raise ValueError("propensities must not be empty")
        total = sum(value.values())
        if total <= 0:
            raise ValueError("propensities must sum to a positive value")
        return value


class DecisionLogStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, entry: DecisionLogEntry) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = entry.model_dump(mode="json")
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    def read_all(self) -> list[DecisionLogEntry]:
        if not self._path.exists():
            return []
        entries: list[DecisionLogEntry] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped == "":
                continue
            payload = json.loads(stripped)
            entries.append(DecisionLogEntry.model_validate(payload))
        return entries

