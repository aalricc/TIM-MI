from __future__ import annotations

import random
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from mi_agent.agent.selector import EpsilonGreedySelector, SelectableAction
from mi_agent.config import MISettings
from mi_agent.policy.store import PolicyStore


def run_learning_curve_smoke(*, episodes: int, seed: int) -> dict[str, Any]:
    if episodes <= 0:
        return {
            "episodes": 0,
            "policy_updates": 0,
            "early_avg_reward": 0.0,
            "late_avg_reward": 0.0,
            "propensity_valid": True,
        }

    settings = MISettings(random_seed=seed)
    selector = EpsilonGreedySelector(settings)
    randomizer = random.Random(seed)
    rewards: list[float] = []
    propensity_valid = True

    with TemporaryDirectory() as tmp_dir:
        policy = PolicyStore(
            path=Path(tmp_dir) / "policy_smoke.json",
            recency_alpha=settings.policy_recency_alpha,
        )
        failure_type = "smoke_learning_curve"

        for visits in range(episodes):
            bad_key = "restart_service|"
            good_key = "apply_schema|"
            candidates = [
                SelectableAction(
                    action_key=bad_key,
                    action_id="restart_service",
                    parameters={},
                    value_score=policy.score(failure_type, bad_key),
                ),
                SelectableAction(
                    action_key=good_key,
                    action_id="apply_schema",
                    parameters={},
                    value_score=policy.score(failure_type, good_key),
                ),
            ]

            distribution = selector.propensity_distribution(
                candidates=candidates,
                visits=visits,
                has_proven_skill=False,
                failed_action_keys=set(),
                state_changed_since_failure=False,
            )
            propensity_sum = sum(distribution.values())
            if abs(propensity_sum - 1.0) > 1e-9:
                propensity_valid = False

            selected, _, _ = selector.select(
                candidates=candidates,
                visits=visits,
                has_proven_skill=False,
                failed_action_keys=set(),
                state_changed_since_failure=False,
            )

            if selected.action_key == good_key:
                reward = 1.0 + (randomizer.random() * 0.05)
            else:
                reward = -1.0 + (randomizer.random() * 0.05)

            selector.observe(action_key=selected.action_key, reward=reward)
            policy.update_action(
                failure_type=failure_type,
                action_key=selected.action_key,
                reward=reward,
                success=reward > 0,
            )
            rewards.append(reward)

    window = max(1, episodes // 5)
    early_avg_reward = sum(rewards[:window]) / window
    late_avg_reward = sum(rewards[-window:]) / window

    return {
        "episodes": episodes,
        "policy_updates": episodes,
        "early_avg_reward": early_avg_reward,
        "late_avg_reward": late_avg_reward,
        "propensity_valid": propensity_valid,
    }
