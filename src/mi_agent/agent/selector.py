from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from mi_agent.config import MISettings


@dataclass(frozen=True, slots=True)
class SelectableAction:
    action_key: str
    action_id: str
    parameters: dict[str, Any]
    value_score: float


class EpsilonGreedySelector:
    def __init__(self, settings: MISettings) -> None:
        self._settings = settings
        self._random = random.Random(settings.random_seed)

    def epsilon(self, *, visits: int, has_proven_skill: bool) -> float:
        base = max(
            self._settings.epsilon_min,
            self._settings.epsilon_start * (self._settings.epsilon_decay**visits),
        )
        if has_proven_skill:
            return max(self._settings.epsilon_min, base * 0.5)
        return base

    def select(
        self,
        *,
        candidates: list[SelectableAction],
        visits: int,
        has_proven_skill: bool,
        failed_action_keys: set[str],
        state_changed_since_failure: bool,
    ) -> tuple[SelectableAction, float, str]:
        filtered = self._filter_repeats(candidates, failed_action_keys, state_changed_since_failure)
        if not filtered:
            raise ValueError("No candidate actions available")
        epsilon = self.epsilon(visits=visits, has_proven_skill=has_proven_skill)
        roll = self._random.random()
        if roll < epsilon:
            pool = self._exploration_pool(filtered)
            choice = self._random.choice(pool)
            return choice, epsilon, "explore"
        best = max(filtered, key=lambda candidate: candidate.value_score)
        return best, epsilon, "exploit"

    @staticmethod
    def _filter_repeats(
        candidates: list[SelectableAction],
        failed_action_keys: set[str],
        state_changed_since_failure: bool,
    ) -> list[SelectableAction]:
        if state_changed_since_failure:
            return list(candidates)
        return [
            candidate for candidate in candidates if candidate.action_key not in failed_action_keys
        ]

    @staticmethod
    def _exploration_pool(candidates: list[SelectableAction]) -> list[SelectableAction]:
        if len(candidates) <= 1:
            return candidates
        sorted_candidates = sorted(candidates, key=lambda c: c.value_score)
        limit = max(1, len(sorted_candidates) // 2)
        return sorted_candidates[:limit]
