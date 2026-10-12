from __future__ import annotations

import json
from pathlib import Path

import pytest

from mi_agent.eval.replay import ReplayLLMClient
from mi_agent.eval.runner import EpisodePlanItem, EvalRunConfig, build_episode_plan, run_replay_eval
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

    has_held_out_scenario = any(item.split == "held_out_scenario" for item in plan)
    has_held_out_variant = any(item.split == "held_out_variant" for item in plan)
    has_drift = any(item.drift_injected for item in plan)

    assert has_held_out_scenario
    assert has_held_out_variant
    assert has_drift


def test_replay_client_returns_recorded_outputs(tmp_path: Path) -> None:
    replay_file = tmp_path / "replay.json"
    replay_file.write_text(
        json.dumps([{"response": "ok-1"}, {"response": "ok-2"}]),
        encoding="utf-8",
    )
    client = ReplayLLMClient(replay_file)

    assert client.complete("prompt-1") == "ok-1"
    assert client.complete("prompt-2") == "ok-2"


def test_run_replay_eval_returns_summary(tmp_path: Path) -> None:
    replay_file = tmp_path / "replay.json"
    replay_file.write_text(
        json.dumps([{"response": "plan"}] * 100),
        encoding="utf-8",
    )
    client = ReplayLLMClient(replay_file)
    config = EvalRunConfig(
        seeds=1,
        curriculum="ordered",
        held_out_scenarios=frozenset({"S18"}),
        held_out_variants=True,
        drift=True,
        shuffle_seed=3,
        base_seed=5,
    )

    summary = run_replay_eval(config, client)

    assert summary["episodes"] > 0
    assert "success_rate" in summary
    assert "split_counts" in summary
    assert isinstance(summary["sample_episode"], EpisodePlanItem)
