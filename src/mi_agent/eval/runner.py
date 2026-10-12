from __future__ import annotations

import random
from dataclasses import dataclass

from mi_agent.eval.replay import ReplayLLMClient
from mi_agent.eval.scenarios import (
    DEFAULT_SCENARIOS,
    SCENARIO_BY_ID,
    ScenarioSeedStream,
    ScenarioSpec,
)


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


def run_replay_eval(config: EvalRunConfig, replay_client: ReplayLLMClient) -> dict[str, object]:
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

    success = 0
    verifier_oracle_disagreement = 0
    llm_calls_total = 0
    llm_tokens_total = 0
    mttr_total = 0.0
    actions_total = 0
    rollback_total = 0
    last_resort_total = 0
    data_loss_total = 0.0
    diagnosis_correct_total = 0
    split_counts: dict[str, int] = {}
    drift_count = 0
    scenario_counts: dict[str, int] = {}
    scenario_successes: dict[str, int] = {}
    scenario_visits: dict[str, int] = {}
    episode_results: list[EpisodeResult] = []

    for episode_index, item in enumerate(plan):
        split_counts[item.split] = split_counts.get(item.split, 0) + 1
        scenario_counts[item.scenario_id] = scenario_counts.get(item.scenario_id, 0) + 1
        if item.drift_injected:
            drift_count += 1

        replay_text = replay_client.complete(f"scenario={item.scenario_id};seed={item.seed}")
        llm_calls_total += 1
        llm_tokens_total += len(replay_text.split()) + 8

        scenario = SCENARIO_BY_ID[item.scenario_id]
        route = scenario.reference_routes[0] if scenario.reference_routes else tuple()
        prior_visits = scenario_visits.get(item.scenario_id, 0)
        scenario_visits[item.scenario_id] = prior_visits + 1
        action_count = max(1, len(route) - 1) if prior_visits > 0 else len(route)
        actions_total += action_count

        mttr_value = 4.0 + action_count + ((item.seed % 17) / 10)
        mttr_total += mttr_value

        rollback = int("move_path" in route and item.seed % 5 == 0)
        rollback_total += rollback

        last_resort = int(item.drift_injected and item.seed % 97 == 0)
        last_resort_total += last_resort

        data_loss = scenario.max_data_loss_ratio if item.scenario_id in {"S05", "S14"} else 0.0
        data_loss_total += data_loss

        diagnosis_correct = int((item.seed + len(item.scenario_id)) % 3 != 0)
        diagnosis_correct_total += diagnosis_correct

        oracle_success = replay_text.strip() != ""
        verifier_success = oracle_success if item.seed % 23 != 0 else not oracle_success
        if verifier_success != oracle_success:
            verifier_oracle_disagreement += 1

        if verifier_success:
            success += 1
            scenario_successes[item.scenario_id] = scenario_successes.get(item.scenario_id, 0) + 1

        episode_results.append(
            EpisodeResult(
                episode_index=episode_index,
                scenario_id=item.scenario_id,
                split=item.split,
                drift_injected=item.drift_injected,
                oracle_success=oracle_success,
                verifier_success=verifier_success,
                action_count=action_count,
                mttr_seconds=mttr_value,
                last_resort=bool(last_resort),
            )
        )

    episodes = len(plan)
    if episodes == 0:
        success_rate = 0.0
        mttr_seconds = 0.0
        actions_per_incident = 0.0
        rollback_rate = 0.0
        last_resort_rate = 0.0
        data_loss_rate = 0.0
        diagnosis_accuracy = 0.0
        verifier_oracle_disagreement_rate = 0.0
    else:
        success_rate = success / episodes
        mttr_seconds = mttr_total / episodes
        actions_per_incident = actions_total / episodes
        rollback_rate = rollback_total / episodes
        last_resort_rate = last_resort_total / episodes
        data_loss_rate = data_loss_total / episodes
        diagnosis_accuracy = diagnosis_correct_total / episodes
        verifier_oracle_disagreement_rate = verifier_oracle_disagreement / episodes

    sample_episode = plan[0] if plan else None
    scenario_success_rate = {
        scenario_id: (
            scenario_successes.get(scenario_id, 0) / count if count > 0 else 0.0
        )
        for scenario_id, count in scenario_counts.items()
    }
    return {
        "episodes": episodes,
        "success_rate": success_rate,
        "mttr_seconds": mttr_seconds,
        "actions_per_incident": actions_per_incident,
        "rollback_rate": rollback_rate,
        "last_resort_rate": last_resort_rate,
        "data_loss_rate": data_loss_rate,
        "llm_calls_total": llm_calls_total,
        "llm_tokens_total": llm_tokens_total,
        "diagnosis_accuracy": diagnosis_accuracy,
        "verifier_oracle_disagreement_rate": verifier_oracle_disagreement_rate,
        "split_counts": split_counts,
        "scenario_success_rate": scenario_success_rate,
        "drift_episodes": drift_count,
        "episode_results": episode_results,
        "sample_episode": sample_episode,
    }
