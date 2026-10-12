from __future__ import annotations

import json
from pathlib import Path

import pytest

from mi_agent.eval import cli
from mi_agent.eval.experiments import ExperimentSuiteConfig, run_replay_experiments
from mi_agent.eval.replay import ReplayLLMClient

pytestmark = pytest.mark.phase8


def _make_replay_file(path: Path, *, responses: int = 4_000) -> Path:
    payload = [{"response": "plan"}] * responses
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_phase8_experiments_require_minimum_seed_count(tmp_path: Path) -> None:
    replay_path = _make_replay_file(tmp_path / "replay.json")
    client = ReplayLLMClient(replay_path)

    with pytest.raises(ValueError, match="seeds >= 5"):
        run_replay_experiments(
            ExperimentSuiteConfig(
                seeds=4,
                curriculum="shuffled",
                held_out_scenarios=frozenset({"S17", "S18"}),
                held_out_variants=True,
                drift=True,
                shuffle_seed=17,
                base_seed=7,
                confidence=0.95,
            ),
            client,
        )


def test_phase8_experiments_include_ablations_confidence_and_safety(tmp_path: Path) -> None:
    replay_path = _make_replay_file(tmp_path / "replay.json")
    client = ReplayLLMClient(replay_path)

    result = run_replay_experiments(
        ExperimentSuiteConfig(
            seeds=5,
            curriculum="shuffled",
            held_out_scenarios=frozenset({"S17", "S18"}),
            held_out_variants=True,
            drift=True,
            shuffle_seed=17,
            base_seed=7,
            confidence=0.95,
        ),
        client,
    )

    assert result["mode"] == "replay"
    conditions = result["conditions"]
    assert isinstance(conditions, dict)
    assert set(conditions.keys()) == {
        "learning_enabled",
        "memory_off",
        "policy_off",
        "skills_off",
    }

    for condition_payload in conditions.values():
        assert isinstance(condition_payload, dict)
        success_ci = condition_payload["success_rate"]
        assert success_ci["ci_low"] <= success_ci["mean"] <= success_ci["ci_high"]
        assert success_ci["n"] == 5.0

    claims = result["claims"]
    assert isinstance(claims, dict)
    assert "beats_no_learning_ablation" in claims
    assert isinstance(claims["beats_no_learning_ablation"], bool)

    red_team = result["red_team"]
    assert isinstance(red_team, dict)
    assert red_team["scenario_id"] == "S18"

    learning_curve = result["learning_curve"]
    assert isinstance(learning_curve, list)
    assert learning_curve


def test_phase8_experiments_cli_requires_replay(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["experiments", "--seeds", "5"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "requires --replay" in captured.err


def test_phase8_experiments_cli_writes_reports(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    replay_path = _make_replay_file(tmp_path / "replay.json")
    output_dir = tmp_path / "reports"

    exit_code = cli.main(
        [
            "experiments",
            "--replay",
            "--replay-file",
            str(replay_path),
            "--seeds",
            "5",
            "--output-dir",
            str(output_dir),
            "--run-id",
            "phase8_suite",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "mi-eval experiments" in captured.out
    assert (output_dir / "phase8_suite.json").exists()
    assert (output_dir / "phase8_suite.md").exists()

    payload = json.loads((output_dir / "phase8_suite.json").read_text(encoding="utf-8"))
    learning_success = payload["conditions"]["learning_enabled"]["success_rate"]["mean"]
    no_learning_success = payload["conditions"]["policy_off"]["success_rate"]["mean"]
    assert learning_success > no_learning_success

