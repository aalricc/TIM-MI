from __future__ import annotations

import json

import pytest

from mi_agent.eval import cli
from mi_agent.eval.learning_curve import run_learning_curve_smoke

pytestmark = pytest.mark.phase5


def test_learning_curve_smoke_shows_improvement() -> None:
    summary = run_learning_curve_smoke(episodes=60, seed=7)

    assert summary["episodes"] == 60
    assert summary["policy_updates"] == 60
    assert summary["propensity_valid"] is True
    assert float(summary["late_avg_reward"]) > float(summary["early_avg_reward"])


def test_mi_eval_learning_smoke_requires_replay(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["learning-smoke"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "requires --replay" in captured.err


def test_mi_eval_learning_smoke_outputs_summary(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["learning-smoke", "--replay", "--episodes", "40", "--seed", "11"])
    captured = capsys.readouterr()

    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["episodes"] == 40
    assert payload["policy_updates"] == 40

