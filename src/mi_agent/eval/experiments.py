from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mi_agent.config import MISettings
from mi_agent.eval.baselines import compute_baselines
from mi_agent.eval.reports import _jsonify
from mi_agent.eval.runner import EvalRunConfig, build_episode_plan, run_live_eval_sync
from mi_agent.eval.scenarios import DEFAULT_SCENARIOS


@dataclass(frozen=True, slots=True)
class ExperimentSuiteConfig:
    seeds: int = 5
    curriculum: str = "shuffled"
    held_out_scenarios: frozenset[str] = frozenset({"S17", "S18"})
    held_out_variants: bool = True
    drift: bool = True
    shuffle_seed: int = 17
    base_seed: int = 7
    execute_live: bool = False


def run_experiments(
    config: ExperimentSuiteConfig,
    settings: MISettings,
) -> dict[str, object]:
    plan = build_episode_plan(
        scenarios=DEFAULT_SCENARIOS,
        seeds_per_scenario=config.seeds,
        curriculum=config.curriculum,
        held_out_scenarios=config.held_out_scenarios,
        include_held_out_variants=config.held_out_variants,
        drift_mode=config.drift,
        shuffle_seed=config.shuffle_seed,
        base_seed=config.base_seed,
    )
    baseline = compute_baselines(plan)

    run_config = EvalRunConfig(
        seeds=config.seeds,
        curriculum=config.curriculum,
        held_out_scenarios=config.held_out_scenarios,
        held_out_variants=config.held_out_variants,
        drift=config.drift,
        shuffle_seed=config.shuffle_seed,
        base_seed=config.base_seed,
        execute_live=config.execute_live,
    )
    run_summary = run_live_eval_sync(run_config, settings)

    payload: dict[str, Any] = {
        "live_run_executed": bool(run_summary.get("live_run_executed", False)),
        "run_summary": run_summary,
        "baseline": baseline,
        "claims": {
            "comparison_possible": bool(run_summary.get("live_run_executed", False)),
        },
    }

    live_success = run_summary.get("success_rate")
    baseline_success = baseline.get("success_rate")
    if isinstance(live_success, (int, float)) and isinstance(baseline_success, (int, float)):
        payload["claims"]["success_rate_delta_vs_baseline"] = float(live_success) - float(
            baseline_success
        )

    if not bool(run_summary.get("live_run_executed", False)):
        payload["claims"]["note"] = "live_run_not_executed"

    return payload


def write_experiment_reports(
    summary: dict[str, object],
    output_dir: Path,
    *,
    run_id: str,
    command: str,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{run_id}.json"
    markdown_path = output_dir / f"{run_id}.md"

    payload = {
        "schema_version": "v1",
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "command": command,
        "summary": _jsonify(summary),
    }
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    lines = [
        "# MI Experiments",
        "",
        f"run_id: {run_id}",
        f"command: {command}",
        "",
        "## Summary",
        f"- live_run_executed: {summary.get('live_run_executed')}",
    ]
    run_summary = summary.get("run_summary")
    if isinstance(run_summary, dict):
        lines.append(f"- episodes: {run_summary.get('episodes')}")
        lines.append(f"- success_rate: {run_summary.get('success_rate')}")

    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path
