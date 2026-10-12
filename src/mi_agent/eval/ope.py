from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

from mi_agent.policy.decision_log import DecisionLogEntry


def load_decision_log(path: Path) -> list[DecisionLogEntry]:
    if not path.exists():
        return []

    entries: list[DecisionLogEntry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == "":
            continue
        payload = json.loads(stripped)
        entries.append(DecisionLogEntry.model_validate(payload))
    return entries


def estimate_off_policy(
    entries: list[DecisionLogEntry],
    *,
    target_policy: str,
) -> dict[str, float | int]:
    if not entries:
        return {
            "count": 0,
            "ips": 0.0,
            "ips_se": 0.0,
            "snips": 0.0,
            "dr": 0.0,
            "dr_se": 0.0,
        }

    q_hat = _fit_reward_model(entries)

    ips_total = 0.0
    snips_num = 0.0
    snips_den = 0.0
    dr_total = 0.0
    ips_samples: list[float] = []
    dr_samples: list[float] = []

    for entry in entries:
        behavior = entry.propensities.get(entry.chosen_action, 0.0)
        if behavior <= 0:
            continue

        target = _target_action_probability(entry, target_policy)
        weight = target / behavior
        reward = entry.reward_components.net_reward

        ips_total += reward * weight
        ips_samples.append(reward * weight)
        snips_num += reward * weight
        snips_den += weight

        target_value = 0.0
        for action in entry.candidate_set:
            probability = _target_probability_for_action(entry, target_policy, action)
            target_value += probability * q_hat.get(action, 0.0)
        dr_component = target_value + (weight * (reward - q_hat.get(entry.chosen_action, 0.0)))
        dr_total += dr_component
        dr_samples.append(dr_component)

    count = len(entries)
    ips = ips_total / count
    snips = 0.0 if snips_den == 0 else snips_num / snips_den
    dr = dr_total / count
    ips_se = _standard_error(ips_samples)
    dr_se = _standard_error(dr_samples)
    return {
        "count": count,
        "ips": ips,
        "ips_se": ips_se,
        "snips": snips,
        "dr": dr,
        "dr_se": dr_se,
    }


def _target_action_probability(entry: DecisionLogEntry, policy: str) -> float:
    return _target_probability_for_action(entry, policy, entry.chosen_action)


def _target_probability_for_action(
    entry: DecisionLogEntry,
    policy: str,
    action: str,
) -> float:
    if policy == "behavior":
        return entry.propensities.get(action, 0.0)
    if policy == "uniform":
        return 1.0 / len(entry.candidate_set)
    if policy == "greedy":
        best_action = max(entry.propensities.items(), key=lambda item: item[1])[0]
        return 1.0 if action == best_action else 0.0
    raise ValueError(f"Unsupported policy: {policy}")


def _fit_reward_model(entries: list[DecisionLogEntry]) -> dict[str, float]:
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for entry in entries:
        action = entry.chosen_action
        reward = entry.reward_components.net_reward
        sums[action] = sums.get(action, 0.0) + reward
        counts[action] = counts.get(action, 0) + 1

    return {
        action: (sums[action] / counts[action])
        for action in sums
        if counts.get(action, 0) > 0
    }


def _standard_error(samples: list[float]) -> float:
    count = len(samples)
    if count <= 1:
        return 0.0
    sample_std = statistics.stdev(samples)
    return sample_std / math.sqrt(count)
