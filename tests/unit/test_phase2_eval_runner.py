from __future__ import annotations

import pytest

from mi_agent.eval.runner import EvalRunConfig, build_episode_plan, run_live_eval_sync
from mi_agent.eval.scenarios import DEFAULT_SCENARIOS

pytestmark = pytest.mark.phase2


def test_build_episode_plan_uses_seed_count_per_scenario() -> None:
    plan = build_episode_plan(
        scenarios=DEFAULT_SCENARIOS,
        seeds_per_scenario=2,
        curriculum="ordered",
        held_out_scenarios=frozenset(),
        include_held_out_variants=False,
        drift_mode=False,
        shuffle_seed=7,
        base_seed=101,
    )

    assert len(plan) == 36
    assert [item.scenario_id for item in plan[:3]] == ["S01", "S01", "S02"]


def test_build_episode_plan_shuffled_curriculum_is_deterministic() -> None:
    a = build_episode_plan(
        scenarios=DEFAULT_SCENARIOS,
        seeds_per_scenario=1,
        curriculum="shuffled",
        held_out_scenarios=frozenset(),
        include_held_out_variants=False,
        drift_mode=False,
        shuffle_seed=11,
        base_seed=42,
    )
    b = build_episode_plan(
        scenarios=DEFAULT_SCENARIOS,
        seeds_per_scenario=1,
        curriculum="shuffled",
        held_out_scenarios=frozenset(),
        include_held_out_variants=False,
        drift_mode=False,
        shuffle_seed=11,
        base_seed=42,
    )
    c = build_episode_plan(
        scenarios=DEFAULT_SCENARIOS,
        seeds_per_scenario=1,
        curriculum="shuffled",
        held_out_scenarios=frozenset(),
        include_held_out_variants=False,
        drift_mode=False,
        shuffle_seed=12,
        base_seed=42,
    )

    assert [item.scenario_id for item in a] == [item.scenario_id for item in b]
    assert [item.scenario_id for item in a] != [item.scenario_id for item in c]


def test_episode_plan_marks_held_out_and_drift_items() -> None:
    plan = build_episode_plan(
        scenarios=DEFAULT_SCENARIOS,
        seeds_per_scenario=1,
        curriculum="ordered",
        held_out_scenarios=frozenset({"S18"}),
        include_held_out_variants=True,
        drift_mode=True,
        shuffle_seed=1,
        base_seed=9,
    )

    assert any(item.split == "held_out_scenario" for item in plan)
    assert any(item.split == "held_out_variant" for item in plan)
    assert any(item.drift_injected for item in plan)


def test_run_live_eval_not_requested_returns_not_run_summary(test_settings) -> None:
    config = EvalRunConfig(
        seeds=1,
        curriculum="ordered",
        held_out_scenarios=frozenset({"S18"}),
        held_out_variants=True,
        drift=True,
        shuffle_seed=3,
        base_seed=5,
        execute_live=False,
    )

    summary = run_live_eval_sync(config, test_settings)

    assert summary["live_run_executed"] is False
    assert summary["episodes"] == 0
    assert summary["episodes_planned"] > 0
    assert summary["failure_categories"]["live_run_not_requested"] > 0
