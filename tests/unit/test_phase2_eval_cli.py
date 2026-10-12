from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mi_agent.eval import cli

pytestmark = pytest.mark.phase2


def _seed_sqlite(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS widgets "
            "(id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
        )
        conn.execute("INSERT OR REPLACE INTO widgets(id, name) VALUES (1, 'seed')")
        conn.commit()


def test_smoke_requires_replay_flag(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["smoke"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "requires --replay" in captured.err


def test_smoke_replay_passes(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["smoke", "--replay"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "PASS" in captured.out


def test_run_requires_replay_flag(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["run", "--seeds", "2"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "requires --replay" in captured.err


def test_run_replay_mode_supports_held_out_and_drift(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    replay_file = tmp_path / "replay.json"
    output_dir = tmp_path / "reports"
    replay_file.write_text(json.dumps([{"response": "plan"}] * 400), encoding="utf-8")

    exit_code = cli.main(
        [
            "run",
            "--replay",
            "--replay-file",
            str(replay_file),
            "--output-dir",
            str(output_dir),
            "--seeds",
            "2",
            "--curriculum",
            "shuffled",
            "--held-out-scenarios",
            "S17,S18",
            "--held-out-variants",
            "--drift",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "episodes=" in captured.out
    assert "held_out_scenario" in captured.out
    assert "drift=" in captured.out
    assert "report_json=" in captured.out
    assert (output_dir / "eval_run.json").exists()
    assert (output_dir / "eval_run.md").exists()


def test_baseline_command_writes_markdown(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    output_file = tmp_path / "BASELINES.md"

    exit_code = cli.main(
        [
            "baseline",
            "--seeds",
            "1",
            "--curriculum",
            "ordered",
            "--output",
            str(output_file),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "mi-eval baseline" in captured.out
    assert output_file.exists()
    assert "MI v2 Baselines" in output_file.read_text(encoding="utf-8")


def test_scenario_command_injects_fault_with_explicit_paths(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    sandbox_root = tmp_path
    app_root = tmp_path / "app"
    backups_root = tmp_path / "backups"
    db_file = app_root / "data" / "app.db"
    _seed_sqlite(db_file)

    exit_code = cli.main(
        [
            "scenario",
            "--id",
            "S01",
            "--seed",
            "11",
            "--sandbox-root",
            str(sandbox_root),
            "--app-root",
            str(app_root),
            "--db-file",
            str(db_file),
            "--backups-root",
            str(backups_root),
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["scenario_id"] == "S01"
    assert payload["seed"] == 11
    assert payload["hidden_label"] == "db_file_missing"
    assert payload["db_file"] == str(db_file)
    assert db_file.exists() is False


def test_scenario_command_rejects_unknown_id(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["scenario", "--id", "S99"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "unknown scenario id" in captured.err
