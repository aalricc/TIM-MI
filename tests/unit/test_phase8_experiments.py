from __future__ import annotations

import json
from pathlib import Path

import pytest

from mi_agent.eval import cli
from mi_agent.eval.experiments import ExperimentSuiteConfig, run_experiments

pytestmark = pytest.mark.phase8


def test_experiments_not_live_returns_not_run_claim(test_settings) -> None:
    result = run_experiments(ExperimentSuiteConfig(seeds=2, execute_live=False), test_settings)
    assert result["live_run_executed"] is False
    assert result["claims"]["comparison_possible"] is False


def test_experiments_cli_writes_reports_without_live(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    exit_code = cli.main(
        [
            "experiments",
            "--seeds",
            "2",
            "--output-dir",
            str(tmp_path),
            "--run-id",
            "phase8_experiments",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "mi-eval experiments" in captured.out
    assert (tmp_path / "phase8_experiments.json").exists()

    payload = json.loads((tmp_path / "phase8_experiments.json").read_text(encoding="utf-8"))
    assert payload["summary"]["live_run_executed"] is False
