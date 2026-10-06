from __future__ import annotations

import json
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from mi_agent.errors import CorruptStoreError
from mi_agent.failure_types import normalize_failure_type
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
    def __init__(self, path: Path) -> None:
        self._path = path
        self._adapter = TypeAdapter(PolicyState)
        self._state = self._load()

    def _load(self) -> PolicyState:
        if not self._path.exists():
            return PolicyState()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            loaded = self._adapter.validate_python(raw)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise CorruptStoreError(f"Corrupt policy store: {self._path}") from exc
        migrated = self._canonicalize_failure_keys(loaded)
        if migrated:
            self._state = loaded
            self._save()
        return loaded

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        serialized = self._state.model_dump(mode="json")
        with NamedTemporaryFile("w", delete=False, dir=self._path.parent, encoding="utf-8") as tmp:
            json.dump(serialized, tmp, ensure_ascii=False, sort_keys=True)
            temp_path = Path(tmp.name)
        temp_path.replace(self._path)

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
        value.avg_reward = ((value.avg_reward * value.visits) + reward) / new_visits
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
