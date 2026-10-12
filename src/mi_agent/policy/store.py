from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from mi_agent.errors import CorruptStoreError
from mi_agent.failure_types import normalize_failure_type
from mi_agent.state.store import JSONFileStateStore
from mi_agent.workflow.models import EpisodeAction


class ActionValue(BaseModel):
    visits: int = 0
    avg_reward: float = 0.0
    success_count: int = 0
    failure_count: int = 0


class FailurePolicy(BaseModel):
    actions: dict[str, ActionValue] = Field(default_factory=dict)
    lessons: list[str] = Field(default_factory=list)


class PolicyState(BaseModel):
    failures: dict[str, FailurePolicy] = Field(default_factory=dict)


class PolicyStore:
    def __init__(self, path: Path, *, recency_alpha: float | None = None) -> None:
        self._path = path
        self._recency_alpha = recency_alpha
        self._adapter = TypeAdapter(PolicyState)
        self._state_store = JSONFileStateStore[PolicyState](
            path,
            default_factory=PolicyState,
            decode=self._decode_state,
            encode=lambda state: state.model_dump(mode="json"),
        )
        self._state = self._load()

    def _load(self) -> PolicyState:
        try:
            loaded = self._state_store.load()
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise CorruptStoreError(f"Corrupt policy store: {self._path}") from exc
        migrated = self._canonicalize_failure_keys(loaded)
        if migrated:
            self._state = loaded
            self._save()
        return loaded

    def _save(self) -> None:
        self._state_store.save(self._state)

    def _decode_state(self, raw: object) -> PolicyState:
        return self._adapter.validate_python(raw)

    def recall_lessons(self, failure_type: str) -> list[str]:
        failure_type = self._canonical_failure_type(failure_type)
        policy = self._state.failures.get(failure_type)
        return list(policy.lessons) if policy is not None else []

    def record_lesson(self, failure_type: str, lesson: str) -> None:
        failure_type = self._canonical_failure_type(failure_type)
        bounded = lesson.strip()[:500]
        policy = self._state.failures.setdefault(failure_type, FailurePolicy())
        policy.lessons.append(bounded)
        policy.lessons = policy.lessons[-20:]
        self._save()

    def value_for(self, failure_type: str, action_key: str) -> ActionValue:
        failure_type = self._canonical_failure_type(failure_type)
        policy = self._state.failures.setdefault(failure_type, FailurePolicy())
        return policy.actions.setdefault(action_key, ActionValue())

    def score(self, failure_type: str, action_key: str) -> float:
        return self.value_for(failure_type, action_key).avg_reward

    def update_action(
        self, failure_type: str, action_key: str, reward: float, success: bool
    ) -> None:
        failure_type = self._canonical_failure_type(failure_type)
        value = self.value_for(failure_type, action_key)
        new_visits = value.visits + 1
        if self._recency_alpha is None:
            value.avg_reward = ((value.avg_reward * value.visits) + reward) / new_visits
        else:
            alpha = max(0.0, min(1.0, self._recency_alpha))
            if value.visits == 0:
                value.avg_reward = reward
            else:
                value.avg_reward = ((1.0 - alpha) * value.avg_reward) + (alpha * reward)
        value.visits = new_visits
        if success:
            value.success_count += 1
        else:
            value.failure_count += 1
        self._save()

    def update_episode(self, failure_type: str, actions: list[EpisodeAction]) -> None:
        for action in actions:
            self.update_action(
                failure_type=failure_type,
                action_key=action.action_key,
                reward=action.reward,
                success=action.success,
            )

    def snapshot(self) -> PolicyState:
        return self._state.model_copy(deep=True)

    @staticmethod
    def _canonical_failure_type(failure_type: str) -> str:
        return normalize_failure_type(failure_type)

    @classmethod
    def _canonicalize_failure_keys(cls, state: PolicyState) -> bool:
        migrated = False
        merged: dict[str, FailurePolicy] = {}
        for failure_type, policy in state.failures.items():
            canonical_type = cls._canonical_failure_type(failure_type)
            existing = merged.get(canonical_type)
            if existing is None:
                merged[canonical_type] = policy.model_copy(deep=True)
                if canonical_type != failure_type:
                    migrated = True
                continue
            migrated = True
            existing.lessons.extend(policy.lessons)
            existing.lessons = existing.lessons[-20:]
            for action_key, value in policy.actions.items():
                current = existing.actions.get(action_key)
                if current is None:
                    existing.actions[action_key] = value.model_copy(deep=True)
                    continue
                total_visits = current.visits + value.visits
                if total_visits > 0:
                    current.avg_reward = (
                        (current.avg_reward * current.visits)
                        + (value.avg_reward * value.visits)
                    ) / total_visits
                current.visits = total_visits
                current.success_count += value.success_count
                current.failure_count += value.failure_count
        if migrated:
            state.failures = merged
        return migrated
