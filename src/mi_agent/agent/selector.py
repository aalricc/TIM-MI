from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from mi_agent.agent.bandit import DiagonalLinUCB
from mi_agent.config import MISettings


@dataclass(frozen=True, slots=True)
class SelectableAction:
    action_key: str
    action_id: str
    parameters: dict[str, Any]
    value_score: float
    shield_state: dict[str, bool] | None = None


class EpsilonGreedySelector:
    def __init__(self, settings: MISettings) -> None:
        self._settings = settings
        self._random = random.Random(settings.random_seed)
        self._bandit = DiagonalLinUCB(dimension=4, alpha=0.7)
        self._pending_features: dict[str, list[float]] = {}

    def epsilon(self, *, visits: int, has_proven_skill: bool) -> float:
        if self._settings.eval_epsilon is not None:
            return self._settings.eval_epsilon
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

        context_vectors = {
            candidate.action_key: self._context_features(
                candidate=candidate,
                visits=visits,
                has_proven_skill=has_proven_skill,
            )
            for candidate in filtered
        }
        scored = {
            candidate.action_key: self._scored_value(
                candidate,
                context_vectors[candidate.action_key],
            )
            for candidate in filtered
        }

        epsilon = self.epsilon(visits=visits, has_proven_skill=has_proven_skill)
        roll = self._random.random()
        if roll < epsilon:
            pool = self._exploration_pool(filtered, scored)
            choice = self._random.choice(pool)
            self._pending_features[choice.action_key] = context_vectors[choice.action_key]
            return choice, epsilon, "explore"
        best = max(
            filtered,
            key=lambda candidate: (scored[candidate.action_key], candidate.action_key),
        )
        self._pending_features[best.action_key] = context_vectors[best.action_key]
        return best, epsilon, "exploit"

    def propensity_distribution(
        self,
        *,
        candidates: list[SelectableAction],
        visits: int,
        has_proven_skill: bool,
        failed_action_keys: set[str],
        state_changed_since_failure: bool,
    ) -> dict[str, float]:
        filtered = self._filter_repeats(candidates, failed_action_keys, state_changed_since_failure)
        if not filtered:
            raise ValueError("No candidate actions available")

        context_vectors = {
            candidate.action_key: self._context_features(
                candidate=candidate,
                visits=visits,
                has_proven_skill=has_proven_skill,
            )
            for candidate in filtered
        }
        scored = {
            candidate.action_key: self._scored_value(
                candidate,
                context_vectors[candidate.action_key],
            )
            for candidate in filtered
        }

        epsilon = self.epsilon(visits=visits, has_proven_skill=has_proven_skill)
        pool = self._exploration_pool(filtered, scored)
        best = max(
            filtered,
            key=lambda candidate: (scored[candidate.action_key], candidate.action_key),
        )
        explore_weight = epsilon / len(pool)

        distribution: dict[str, float] = {}
        for candidate in filtered:
            probability = 0.0
            if candidate.action_key == best.action_key:
                probability += 1.0 - epsilon
            if candidate in pool:
                probability += explore_weight
            distribution[candidate.action_key] = probability

        total = sum(distribution.values())
        if total <= 0:
            raise ValueError("Invalid propensity distribution")
        return {key: value / total for key, value in distribution.items()}

    def observe(
        self,
        *,
        action_key: str,
        reward: float,
        features: list[float] | None = None,
    ) -> None:
        context = features
        if context is None:
            context = self._pending_features.pop(action_key, None)
        if context is None:
            context = [1.0, 0.0, 0.0, 0.0]
        self._bandit.update(action_key, context, reward)

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

    def _context_features(
        self,
        *,
        candidate: SelectableAction,
        visits: int,
        has_proven_skill: bool,
    ) -> list[float]:
        normalized_visits = min(1.0, visits / 50.0)
        return [
            1.0,
            normalized_visits,
            1.0 if has_proven_skill else 0.0,
            candidate.value_score,
        ]

    def _scored_value(self, candidate: SelectableAction, features: list[float]) -> float:
        return candidate.value_score + self._bandit.score(candidate.action_key, features)

    @staticmethod
    def _exploration_pool(
        candidates: list[SelectableAction],
        scores: dict[str, float],
    ) -> list[SelectableAction]:
        if len(candidates) <= 1:
            return candidates
        sorted_candidates = sorted(candidates, key=lambda c: scores[c.action_key])
        limit = max(1, len(sorted_candidates) // 2)
        return sorted_candidates[:limit]
