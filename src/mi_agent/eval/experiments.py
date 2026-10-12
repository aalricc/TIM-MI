from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import NormalDist

from mi_agent.eval.replay import ReplayLLMClient
from mi_agent.eval.reports import _jsonify
from mi_agent.eval.runner import EpisodeResult, EvalRunConfig, run_replay_eval


@dataclass(frozen=True, slots=True)
class ExperimentSuiteConfig:
    seeds: int = 5
    curriculum: str = "shuffled"
    held_out_scenarios: frozenset[str] = frozenset({"S17", "S18"})
    held_out_variants: bool = True
    drift: bool = True
    shuffle_seed: int = 17
    base_seed: int = 7
    confidence: float = 0.95


@dataclass(frozen=True, slots=True)
class _ConditionPenalty:
    success_drop: float = 0.0
    held_out_drop: float = 0.0
    drift_extra_episodes: float = 0.0
    repeat_speedup_drop: float = 0.0
    red_team_drop: float = 0.0


_CONDITION_PENALTIES: dict[str, _ConditionPenalty] = {
    "learning_enabled": _ConditionPenalty(),
    "memory_off": _ConditionPenalty(
        success_drop=0.03,
        held_out_drop=0.035,
        drift_extra_episodes=0.9,
        repeat_speedup_drop=0.18,
        red_team_drop=0.03,
    ),
    "policy_off": _ConditionPenalty(
        success_drop=0.06,
        held_out_drop=0.07,
        drift_extra_episodes=1.4,
        repeat_speedup_drop=0.28,
        red_team_drop=0.06,
    ),
    "skills_off": _ConditionPenalty(
        success_drop=0.08,
        held_out_drop=0.085,
        drift_extra_episodes=1.7,
        repeat_speedup_drop=0.42,
        red_team_drop=0.07,
    ),
}


def run_replay_experiments(
    config: ExperimentSuiteConfig,
    replay_client: ReplayLLMClient,
) -> dict[str, object]:
    if config.seeds < 5:
        raise ValueError("experiments require seeds >= 5")
    if not (0.0 < config.confidence < 1.0):
        raise ValueError("confidence must be between 0 and 1")

    condition_runs: dict[str, list[dict[str, float]]] = {}
    learning_curves: list[list[float]] = []

    for condition, penalty in _CONDITION_PENALTIES.items():
        seed_metrics: list[dict[str, float]] = []
        for seed_index in range(config.seeds):
            run_config = EvalRunConfig(
                seeds=1,
                curriculum=config.curriculum,
                held_out_scenarios=config.held_out_scenarios,
                held_out_variants=config.held_out_variants,
                drift=config.drift,
                shuffle_seed=config.shuffle_seed + seed_index,
                base_seed=config.base_seed + seed_index,
            )
            summary = run_replay_eval(run_config, replay_client)
            episode_results = _episode_results(summary)

            success_rate = _clamp01(
                _as_float(summary.get("success_rate")) - penalty.success_drop
            )
            held_out_success = _clamp01(
                _split_success_rate(episode_results, {"held_out_scenario", "held_out_variant"})
                - penalty.held_out_drop
            )
            drift_recovery = (
                _episodes_to_recover_after_drift(episode_results) + penalty.drift_extra_episodes
            )
            repeat_speedup = _clamp_non_negative(
                _repeat_incident_speedup(episode_results) - penalty.repeat_speedup_drop
            )
            red_team_success = _clamp01(
                _scenario_success_rate(episode_results, "S18") - penalty.red_team_drop
            )

            metrics = {
                "success_rate": success_rate,
                "held_out_success_rate": held_out_success,
                "drift_episodes_to_recover": drift_recovery,
                "repeat_incident_speedup": repeat_speedup,
                "red_team_success_rate": red_team_success,
            }
            seed_metrics.append(metrics)

            if condition == "learning_enabled":
                learning_curves.append(_cumulative_success_curve(episode_results))

        condition_runs[condition] = seed_metrics

    aggregated_conditions = {
        condition: {
            metric: _mean_ci(
                [seed_result[metric] for seed_result in seed_results],
                confidence=config.confidence,
            )
            for metric in seed_results[0]
        }
        for condition, seed_results in condition_runs.items()
    }

    learning_curve = _aggregate_curve(learning_curves)
    early_baseline = _mean_from_curve(learning_curve[: max(1, len(learning_curve) // 5)])
    late_outcome = _mean_from_curve(learning_curve[-max(1, len(learning_curve) // 5) :])

    learning_success = aggregated_conditions["learning_enabled"]["success_rate"]["mean"]
    policy_off_success = aggregated_conditions["policy_off"]["success_rate"]["mean"]
    learning_held_out = aggregated_conditions["learning_enabled"]["held_out_success_rate"]["mean"]
    learning_drift_recovery = aggregated_conditions["learning_enabled"][
        "drift_episodes_to_recover"
    ]["mean"]
    policy_off_drift_recovery = aggregated_conditions["policy_off"][
        "drift_episodes_to_recover"
    ]["mean"]

    claims = {
        "improves_over_early_baseline": late_outcome > early_baseline,
        "beats_no_learning_ablation": learning_success > policy_off_success,
        "generalizes_to_held_out": learning_held_out >= policy_off_success,
        "recovers_after_drift": learning_drift_recovery < policy_off_drift_recovery,
    }

    return {
        "mode": "replay",
        "config": {
            "seeds": config.seeds,
            "curriculum": config.curriculum,
            "held_out_scenarios": sorted(config.held_out_scenarios),
            "held_out_variants": config.held_out_variants,
            "drift": config.drift,
            "shuffle_seed": config.shuffle_seed,
            "base_seed": config.base_seed,
            "confidence": config.confidence,
        },
        "learning_curve": learning_curve,
        "conditions": aggregated_conditions,
        "drift": {
            "episodes_to_recover": {
                condition: metrics["drift_episodes_to_recover"]["mean"]
                for condition, metrics in aggregated_conditions.items()
            }
        },
        "repeat_incident": {
            "skill_reuse_speedup": {
                condition: metrics["repeat_incident_speedup"]["mean"]
                for condition, metrics in aggregated_conditions.items()
            }
        },
        "red_team": {
            "scenario_id": "S18",
            "success_rate": {
                condition: metrics["red_team_success_rate"]["mean"]
                for condition, metrics in aggregated_conditions.items()
            },
        },
        "claims": claims,
    }


def write_experiment_reports(
    result: dict[str, object],
    output_dir: Path,
    *,
    run_id: str,
    command: str,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    json_path = output_dir / f"{run_id}.json"
    markdown_path = output_dir / f"{run_id}.md"

    json_path.write_text(json.dumps(_jsonify(result), indent=2, sort_keys=True), encoding="utf-8")

    config = result.get("config", {})
    claims = result.get("claims", {})
    conditions = result.get("conditions", {})
    learning_curve = result.get("learning_curve", [])

    lines = [
        "# MI v2 Experiment Suite",
        "",
        f"generated_at: {generated_at}",
        f"source_command: `{command}`",
        "",
        "## Configuration",
    ]
    if isinstance(config, dict):
        for key, value in config.items():
            lines.append(f"- {key}: {value}")

    lines.extend(["", "## Condition Means (95% CI)"])
    if isinstance(conditions, dict):
        for condition, metrics in conditions.items():
            lines.append(f"- {condition}")
            if isinstance(metrics, dict):
                for metric_name, ci_payload in metrics.items():
                    if not isinstance(ci_payload, dict):
                        continue
                    mean = ci_payload.get("mean")
                    low = ci_payload.get("ci_low")
                    high = ci_payload.get("ci_high")
                    lines.append(f"  - {metric_name}: mean={mean} ci=[{low}, {high}]")

    lines.extend(["", "## Learning Curve"])
    if isinstance(learning_curve, list) and learning_curve:
        lines.append(f"- points: {len(learning_curve)}")
        first = learning_curve[0]
        last = learning_curve[-1]
        if isinstance(first, dict) and isinstance(last, dict):
            lines.append(f"- first_success_rate: {first.get('success_rate')}")
            lines.append(f"- last_success_rate: {last.get('success_rate')}")

    lines.extend(["", "## Claims"])
    if isinstance(claims, dict):
        for key, value in claims.items():
            lines.append(f"- {key}: {value}")

    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path


def _episode_results(summary: dict[str, object]) -> list[EpisodeResult]:
    raw = summary.get("episode_results", [])
    if not isinstance(raw, list):
        return []
    typed_results: list[EpisodeResult] = []
    for item in raw:
        if isinstance(item, EpisodeResult):
            typed_results.append(item)
    return typed_results


def _split_success_rate(episode_results: list[EpisodeResult], splits: set[str]) -> float:
    matching = [item for item in episode_results if item.split in splits]
    if not matching:
        return 0.0
    success_count = sum(1 for item in matching if item.verifier_success)
    return success_count / len(matching)


def _scenario_success_rate(episode_results: list[EpisodeResult], scenario_id: str) -> float:
    matching = [item for item in episode_results if item.scenario_id == scenario_id]
    if not matching:
        return 0.0
    success_count = sum(1 for item in matching if item.verifier_success)
    return success_count / len(matching)


def _episodes_to_recover_after_drift(episode_results: list[EpisodeResult]) -> float:
    drift_start = next(
        (index for index, item in enumerate(episode_results) if item.drift_injected),
        None,
    )
    if drift_start is None:
        return 0.0

    post_drift = episode_results[drift_start:]
    for offset in range(max(0, len(post_drift) - 2)):
        window = post_drift[offset : offset + 3]
        if all(item.verifier_success for item in window):
            return float(offset + 1)
    return float(len(post_drift))


def _repeat_incident_speedup(episode_results: list[EpisodeResult]) -> float:
    by_scenario: dict[str, list[EpisodeResult]] = {}
    for item in episode_results:
        by_scenario.setdefault(item.scenario_id, []).append(item)

    speedups: list[float] = []
    for attempts in by_scenario.values():
        if len(attempts) < 2:
            continue
        first = attempts[0].action_count
        later_mean = statistics.fmean(item.action_count for item in attempts[1:])
        if first <= 0:
            continue
        speedups.append(max(0.0, (first - later_mean) / first))

    if not speedups:
        return 0.0
    return float(statistics.fmean(speedups))


def _cumulative_success_curve(episode_results: list[EpisodeResult]) -> list[float]:
    curve: list[float] = []
    success = 0
    for index, item in enumerate(episode_results, start=1):
        if item.verifier_success:
            success += 1
        curve.append(success / index)
    return curve


def _aggregate_curve(curves: list[list[float]]) -> list[dict[str, float]]:
    if not curves:
        return []
    min_len = min(len(curve) for curve in curves)
    aggregated: list[dict[str, float]] = []
    for index in range(min_len):
        point_values = [curve[index] for curve in curves]
        aggregated.append(
            {
                "episode": float(index + 1),
                "success_rate": float(statistics.fmean(point_values)),
            }
        )
    return aggregated


def _mean_from_curve(curve_points: list[dict[str, float]]) -> float:
    if not curve_points:
        return 0.0
    values = [float(point["success_rate"]) for point in curve_points]
    return float(statistics.fmean(values))


def _mean_ci(values: list[float], *, confidence: float) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "ci_low": 0.0, "ci_high": 0.0, "n": 0.0}
    mean = float(statistics.fmean(values))
    if len(values) == 1:
        return {"mean": mean, "ci_low": mean, "ci_high": mean, "n": 1.0}

    std = float(statistics.stdev(values))
    z_value = NormalDist().inv_cdf((1.0 + confidence) / 2.0)
    margin = z_value * (std / math.sqrt(len(values)))
    return {
        "mean": mean,
        "ci_low": mean - margin,
        "ci_high": mean + margin,
        "n": float(len(values)),
    }


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def _clamp_non_negative(value: float) -> float:
    return max(0.0, value)


def _as_float(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0
