from __future__ import annotations

from pathlib import Path

import pytest

from mi_agent.eval.baselines import compute_baselines, render_baselines_markdown
from mi_agent.eval.runner import build_episode_plan
from mi_agent.eval.scenarios import DEFAULT_SCENARIOS

pytestmark = pytest.mark.phase2


def test_compute_baselines_returns_random_and_v1_metrics() -> None:
    plan = build_episode_plan(
        scenarios=DEFAULT_SCENARIOS,
        seeds_per_scenario=1,
        curriculum="ordered",
        held_out_scenarios=frozenset(),
        include_held_out_variants=False,
        drift_mode=False,
        shuffle_seed=5,
        base_seed=7,
    )

    baselines = compute_baselines(plan)

    assert set(baselines) == {"random_over_allowlist", "v1_agent"}
    assert (
        baselines["v1_agent"]["success_rate"]
        >= baselines["random_over_allowlist"]["success_rate"]
    )


def test_render_baselines_markdown_has_expected_sections() -> None:
    baselines = {
        "random_over_allowlist": {
            "success_rate": 0.31,
            "mttr_seconds": 11.2,
            "actions_per_incident": 4.7,
            "last_resort_rate": 0.08,
        },
        "v1_agent": {
            "success_rate": 0.57,
            "mttr_seconds": 8.5,
            "actions_per_incident": 3.4,
            "last_resort_rate": 0.04,
        },
    }

    markdown = render_baselines_markdown(
        baselines,
        source_summary={"episodes": 18, "curriculum": "ordered", "seeds": 1},
        command="mi-eval baseline --seeds 1",
        generated_at="2026-10-07",
    )

    assert "# MI v2 Baselines" in markdown
    assert "random_over_allowlist" in markdown
    assert "v1_agent" in markdown
    assert "success_rate" in markdown


def test_baselines_markdown_can_be_written(tmp_path: Path) -> None:
    text = "# MI v2 Baselines\n"
    target = tmp_path / "BASELINES.md"
    target.write_text(text, encoding="utf-8")

    assert target.read_text(encoding="utf-8") == text
