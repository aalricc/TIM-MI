from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(slots=True)
class _ActionState:
    precision: list[float]
    weighted_reward: list[float]


@dataclass(slots=True)
class DiagonalLinUCB:
    dimension: int
    alpha: float = 1.0
    _state: dict[str, _ActionState] = field(default_factory=dict)

    def score(self, action_key: str, features: list[float]) -> float:
        state = self._state.setdefault(
            action_key,
            _ActionState(
                precision=[1.0] * self.dimension,
                weighted_reward=[0.0] * self.dimension,
            ),
        )
        means = [
            state.weighted_reward[index] / state.precision[index]
            for index in range(self.dimension)
        ]
        mean_reward = sum(means[index] * features[index] for index in range(self.dimension))
        uncertainty = math.sqrt(
            sum((features[index] ** 2) / state.precision[index] for index in range(self.dimension))
        )
        return mean_reward + (self.alpha * uncertainty)

    def update(self, action_key: str, features: list[float], reward: float) -> None:
        state = self._state.setdefault(
            action_key,
            _ActionState(
                precision=[1.0] * self.dimension,
                weighted_reward=[0.0] * self.dimension,
            ),
        )
        for index, value in enumerate(features):
            state.precision[index] += value * value
            state.weighted_reward[index] += reward * value

