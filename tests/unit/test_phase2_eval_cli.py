from __future__ import annotations

import json
from pathlib import Path

import pytest

from mi_agent.eval import cli

pytestmark = pytest.mark.phase2


def test_smoke_offline_passes(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["smoke"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "PASS" in captured.out


def test_run_writes_report_without_live_execution(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    exit_code = cli.main(
        [
            "run",
            "--seeds",
            "1",
            "--output-dir",
            str(tmp_path),
            "--run-id",
            "phase2_run",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "mi-eval run" in captured.out

    payload = json.loads((tmp_path / "phase2_run.json").read_text(encoding="utf-8"))
    assert payload["live_run_executed"] is False


def test_baseline_writes_markdown(tmp_path: Path) -> None:
    out = tmp_path / "BASELINES.md"
    exit_code = cli.main(["baseline", "--output", str(out)])
    assert exit_code == 0
    assert out.exists()


def test_learning_smoke_outputs_summary(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["learning-smoke", "--episodes", "40", "--seed", "11"])
    captured = capsys.readouterr()

    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["episodes"] == 40
    assert payload["policy_updates"] == 40
