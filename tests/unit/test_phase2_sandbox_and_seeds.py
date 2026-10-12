from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from mi_agent.eval.sandbox import SandboxResetter
from mi_agent.eval.scenarios import DEFAULT_SCENARIOS, ScenarioSeedStream

pytestmark = pytest.mark.phase2


def test_seed_stream_is_deterministic_across_instances() -> None:
    a = ScenarioSeedStream(base_seed=123)
    b = ScenarioSeedStream(base_seed=123)

    seq_a = [a.seed_for("S01", episode=i) for i in range(5)]
    seq_b = [b.seed_for("S01", episode=i) for i in range(5)]
    seq_other = [a.seed_for("S02", episode=i) for i in range(5)]

    assert seq_a == seq_b
    assert seq_a != seq_other


def test_default_scenarios_cover_s01_to_s18() -> None:
    ids = [scenario.scenario_id for scenario in DEFAULT_SCENARIOS]
    assert ids == [f"S{i:02d}" for i in range(1, 19)]


def test_default_scenarios_include_fault_oracle_and_trap_metadata() -> None:
    for scenario in DEFAULT_SCENARIOS:
        assert scenario.injected_fault.strip() != ""
        assert scenario.end_state_oracle.strip() != ""
        assert scenario.trap.strip() != ""
        assert scenario.parameter_keys == ("db_size", "timing", "target", "seed")


def test_known_scenario_metadata_matches_spec_examples() -> None:
    by_id = {scenario.scenario_id: scenario for scenario in DEFAULT_SCENARIOS}

    assert by_id["S01"].injected_fault == "Delete DB file"
    assert by_id["S15"].trap == "Any mutation"
    assert "instruction-like text" in by_id["S18"].injected_fault


def test_pyproject_exposes_mi_eval_console_script() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    scripts = pyproject.get("project", {}).get("scripts", {})
    assert scripts.get("mi-eval") == "mi_agent.eval.cli:main"


def test_sandbox_resetter_restores_baseline_state(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    sandbox = tmp_path / "sandbox"

    (baseline / "app").mkdir(parents=True)
    (baseline / "app" / "state.txt").write_text("baseline", encoding="utf-8")

    resetter = SandboxResetter(baseline_root=baseline, sandbox_root=sandbox)
    resetter.reset()

    (sandbox / "app" / "state.txt").write_text("modified", encoding="utf-8")
    (sandbox / "app" / "extra.txt").write_text("extra", encoding="utf-8")

    resetter.reset()

    assert (sandbox / "app" / "state.txt").read_text(encoding="utf-8") == "baseline"
    assert not (sandbox / "app" / "extra.txt").exists()
