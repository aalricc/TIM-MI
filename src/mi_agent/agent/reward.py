from __future__ import annotations

from mi_agent.policy.decision_log import RewardComponents

_TIER_PENALTY = {
    "read_only": 0.1,
    "reversible": 0.3,
    "irreversible": 0.8,
    "terminal": 1.0,
    "brokered": 0.6,
}


def score_outcome(
    *,
    verifier_healthy: bool,
    duration_seconds: float,
    attempts: int,
    risk_tier: str,
    rolled_back: bool,
    used_last_resort: bool,
    data_loss_ratio: float,
) -> RewardComponents:
    verifier_success = 10.0 if verifier_healthy else 0.0
    duration_penalty = max(0.0, duration_seconds / 60.0)
    attempt_penalty = max(0.0, float(attempts))
    action_penalty = _TIER_PENALTY.get(risk_tier, 0.5)
    rollback_penalty = 1.0 if rolled_back else 0.0
    last_resort_penalty = 2.0 if used_last_resort else 0.0
    data_loss_penalty = max(0.0, data_loss_ratio) * 10.0

    total_penalty = (
        duration_penalty
        + attempt_penalty
        + action_penalty
        + rollback_penalty
        + last_resort_penalty
        + data_loss_penalty
    )
    net_reward = verifier_success - total_penalty
    return RewardComponents(
        verifier_success=verifier_success,
        duration_penalty=duration_penalty,
        attempt_penalty=attempt_penalty,
        rollback_penalty=rollback_penalty,
        last_resort_penalty=last_resort_penalty,
        terminal_penalty=action_penalty,
        net_reward=net_reward,
    )

