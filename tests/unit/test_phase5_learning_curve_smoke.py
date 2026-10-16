from __future__ import annotations

import json

import pytest

from mi_agent.eval import cli
from mi_agent.eval.learning_curve import run_learning_curve_smoke

pytestmark = pytest.mark.phase5


def test_learning_curve_smoke_handles_zero_episodes() -> None:
    summary = run_learning_curve_smoke(episodes=0, seed=7)
    assert summary["episodes"] == 0
    assert summary["policy_updates"] == 0


def test_mi_eval_learning_smoke_outputs_summary(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["learning-smoke", "--episodes", "40", "--seed", "11"])
    captured = capsys.readouterr()

    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["episodes"] == 40
    assert payload["propensity_valid"] is True
