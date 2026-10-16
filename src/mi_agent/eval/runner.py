from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from pathlib import Path
from shutil import copytree, rmtree
from tempfile import TemporaryDirectory

from mi_agent.config import MISettings
from mi_agent.eval.injectors import InjectorContext, run_injector
from mi_agent.eval.oracles import OracleInput, evaluate_end_state
from mi_agent.eval.scenarios import (
    DEFAULT_SCENARIOS,
    ScenarioSeedStream,
    ScenarioSpec,
)
from mi_agent.runtime import build_agent


@dataclass(frozen=True, slots=True)
class EpisodePlanItem:
    scenario_id: str
    seed: int
    split: str
    drift_injected: bool


@dataclass(frozen=True, slots=True)
class EvalRunConfig:
    seeds: int
    curriculum: str
    held_out_scenarios: frozenset[str]
    held_out_variants: bool
    drift: bool
    shuffle_seed: int
    base_seed: int
    execute_live: bool = False


@dataclass(frozen=True, slots=True)
class EpisodeResult:
    episode_index: int
    scenario_id: str
    split: str
    drift_injected: bool
    oracle_success: bool
    verifier_success: bool
    action_count: int
    mttr_seconds: float
    last_resort: bool
    retry_count: int


def build_episode_plan(
    *,
    scenarios: tuple[ScenarioSpec, ...],
    seeds_per_scenario: int,
    curriculum: str,
    held_out_scenarios: frozenset[str],
    include_held_out_variants: bool,
    drift_mode: bool,
    shuffle_seed: int,
    base_seed: int,
) -> list[EpisodePlanItem]:
    stream = ScenarioSeedStream(base_seed)
    items: list[EpisodePlanItem] = []

    for scenario in scenarios:
        split = "held_out_scenario" if scenario.scenario_id in held_out_scenarios else "train"
        for episode_idx in range(seeds_per_scenario):
            items.append(
                EpisodePlanItem(
                    scenario_id=scenario.scenario_id,
                    seed=stream.seed_for(scenario.scenario_id, episode=episode_idx),
                    split=split,
                    drift_injected=False,
                )
            )
        if include_held_out_variants:
            items.append(
                EpisodePlanItem(
                    scenario_id=scenario.scenario_id,
                    seed=stream.seed_for(scenario.scenario_id, episode=9_999),
                    split="held_out_variant",
                    drift_injected=False,
                )
            )

    if curriculum == "shuffled":
        randomizer = random.Random(shuffle_seed)
        randomizer.shuffle(items)

    if drift_mode and items:
        drift_start = len(items) // 2
        items = [
            EpisodePlanItem(
                scenario_id=item.scenario_id,
                seed=item.seed,
                split=item.split,
                drift_injected=idx >= drift_start,
            )
            for idx, item in enumerate(items)
        ]
    return items


async def run_live_eval(config: EvalRunConfig, settings: MISettings) -> dict[str, object]:
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

    if not config.execute_live:
        return {
            "live_run_executed": False,
            "reason": "live_run_not_requested",
            "episodes": 0,
            "episodes_planned": len(plan),
            "split_counts": _split_counts(plan),
            "scenario_success_rate": {},
            "failure_categories": {"live_run_not_requested": len(plan)},
            "retry_counts": {"total": 0},
            "episode_results": [],
            "sample_episode": plan[0] if plan else None,
        }

    if not settings.sandbox_root.exists():
        raise FileNotFoundError(f"Sandbox root does not exist: {settings.sandbox_root}")

    llm_client = build_agent(settings)[1].llm
    preflight = await llm_client.preflight()
    print(
        "mi-eval preflight: "
        f"model={preflight.get('model')} "
        f"host={preflight.get('endpoint_host')} "
        f"status={preflight.get('status')} "
        f"summary={preflight.get('summary')}"
    )

    episode_results: list[EpisodeResult] = []
    scenario_counts: dict[str, int] = {}
    scenario_successes: dict[str, int] = {}
    failure_categories: dict[str, int] = {}
    total_actions = 0
    total_mttr = 0.0
    total_retry_count = 0
    llm_calls_total = 0

    with TemporaryDirectory(prefix="mi-live-eval-") as temp_root:
        temp_root_path = Path(temp_root)
        baseline_sandbox = temp_root_path / "baseline"
        copytree(settings.sandbox_root, baseline_sandbox)

        for episode_index, item in enumerate(plan):
            episode_sandbox = temp_root_path / f"episode-{episode_index}"
            if episode_sandbox.exists():
                rmtree(episode_sandbox)
            copytree(baseline_sandbox, episode_sandbox)

            episode_settings = settings.model_copy(
                update={
                    "sandbox_root": episode_sandbox,
                    "app_root": episode_sandbox / "app",
                    "backups_root": episode_sandbox / "backups",
                    "db_file": episode_sandbox / "app" / "data" / "app.db",
                    "app_log": episode_sandbox / "app" / "app.log",
                    "schema_file": episode_sandbox / "app" / "schema.sql",
                    "state_dir": episode_sandbox / "state",
                    "policy_store_path": episode_sandbox / "state" / "policy_store.json",
                    "skill_store_path": (
                        episode_sandbox / "state" / "global_knowledge" / "skills.md"
                    ),
                    "decision_log_path": episode_sandbox / "state" / "decision_log.jsonl",
                    "event_log_path": episode_sandbox / "state" / "events.jsonl",
                    "failure_taxonomy_path": episode_sandbox / "state" / "failure_taxonomy.json",
                }
            )

            context = InjectorContext(
                sandbox_root=episode_settings.sandbox_root,
                app_root=episode_settings.app_root,
                db_file=episode_settings.db_file,
                backups_root=episode_settings.backups_root,
            )
            run_injector(item.scenario_id, context, seed=item.seed)

            agent, runtime_context = build_agent(episode_settings)
            usage_before = getattr(runtime_context.llm, "usage", None)
            calls_before = int(getattr(usage_before, "calls", 0)) if usage_before else 0

            started = time.perf_counter()
            await agent._recover_loop()
            mttr_seconds = max(0.0, time.perf_counter() - started)

            verify_result = await runtime_context.verifier.verify(failure_type=item.scenario_id)
            decision_entries = runtime_context.decision_log.read_all()
            actions = tuple(entry.chosen_action for entry in decision_entries)
            retry_count = sum(
                1
                for event in runtime_context.events.events
                if event.event_type in {"llm.diagnose_retry", "llm.plan_retry"}
            )
            total_retry_count += retry_count
            total_actions += len(actions)
            total_mttr += mttr_seconds

            oracle = evaluate_end_state(
                OracleInput(
                    scenario_id=item.scenario_id,
                    db_file=episode_settings.db_file,
                    integrity_ok=bool(verify_result.details.get("integrity_ok", False)),
                    schema_ok=bool(verify_result.details.get("schema_ok", False)),
                    app_healthy=bool(verify_result.details.get("health_ok", False)),
                    actions=actions,
                    ledger_entries=tuple(),
                )
            )

            scenario_counts[item.scenario_id] = scenario_counts.get(item.scenario_id, 0) + 1
            if verify_result.healthy:
                scenario_successes[item.scenario_id] = (
                    scenario_successes.get(item.scenario_id, 0) + 1
                )
            else:
                failure_categories["verifier_unhealthy"] = (
                    failure_categories.get("verifier_unhealthy", 0) + 1
                )
            if not oracle.passed:
                for penalty in oracle.penalties:
                    failure_categories[penalty] = failure_categories.get(penalty, 0) + 1

            usage_after = getattr(runtime_context.llm, "usage", None)
            calls_after = int(getattr(usage_after, "calls", 0)) if usage_after else calls_before
            llm_calls_total += max(0, calls_after - calls_before)

            episode_results.append(
                EpisodeResult(
                    episode_index=episode_index,
                    scenario_id=item.scenario_id,
                    split=item.split,
                    drift_injected=item.drift_injected,
                    oracle_success=oracle.passed,
                    verifier_success=verify_result.healthy,
                    action_count=len(actions),
                    mttr_seconds=mttr_seconds,
                    last_resort=False,
                    retry_count=retry_count,
                )
            )

    episodes = len(episode_results)
    success_count = sum(1 for item in episode_results if item.verifier_success)
    success_rate = (success_count / episodes) if episodes > 0 else 0.0
    scenario_success_rate = {
        scenario_id: (
            scenario_successes.get(scenario_id, 0) / count if count > 0 else 0.0
        )
        for scenario_id, count in scenario_counts.items()
    }

    return {
        "live_run_executed": True,
        "episodes": episodes,
        "episodes_planned": len(plan),
        "success_rate": success_rate,
        "mttr_seconds": (total_mttr / episodes) if episodes > 0 else 0.0,
        "actions_per_incident": (total_actions / episodes) if episodes > 0 else 0.0,
        "llm_calls_total": llm_calls_total,
        "split_counts": _split_counts(plan),
        "scenario_success_rate": scenario_success_rate,
        "failure_categories": failure_categories,
        "retry_counts": {"total": total_retry_count},
        "episode_results": episode_results,
        "sample_episode": plan[0] if plan else None,
    }


def run_live_eval_sync(config: EvalRunConfig, settings: MISettings) -> dict[str, object]:
    return asyncio.run(run_live_eval(config, settings))


def _split_counts(plan: list[EpisodePlanItem]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in plan:
        counts[item.split] = counts.get(item.split, 0) + 1
    return counts
