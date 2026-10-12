from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.phase8


def _seed_sqlite(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS widgets "
            "(id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
        )
        conn.execute("INSERT OR REPLACE INTO widgets(id, name) VALUES (1, 'seed')")
        conn.commit()


def test_phase8_scenarios_script_exists_and_is_executable() -> None:
    script = Path("scripts/v2_scenarios.sh")
    assert script.exists()
    assert script.stat().st_mode & 0o111


def test_phase8_scenarios_script_runs_single_scenario(tmp_path: Path) -> None:
    sandbox_root = tmp_path / "sandbox"
    app_root = sandbox_root / "app"
    db_file = app_root / "data" / "app.db"
    backups_root = sandbox_root / "backups"
    output_jsonl = tmp_path / "scenario_injections.jsonl"
    _seed_sqlite(db_file)

    env = dict(
        **os.environ,
        MI_BASE_SEED="11",
        MI_SANDBOX_ROOT=str(sandbox_root),
        MI_APP_ROOT=str(app_root),
        MI_DB_FILE=str(db_file),
        MI_BACKUPS_ROOT=str(backups_root),
    )
    result = subprocess.run(
        ["bash", "scripts/v2_scenarios.sh", str(output_jsonl), "S01"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert "scenario injection run complete" in result.stdout
    assert output_jsonl.exists()
    lines = [line for line in output_jsonl.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 1

    payload = json.loads(lines[0])
    assert payload["scenario_id"] == "S01"
    assert payload["seed"] == 11
    assert payload["hidden_label"] == "db_file_missing"
    assert db_file.exists() is False


def test_phase8_scenarios_script_resets_baseline_between_scenarios(tmp_path: Path) -> None:
    sandbox_root = tmp_path / "sandbox"
    app_root = sandbox_root / "app"
    db_file = app_root / "data" / "app.db"
    backups_root = sandbox_root / "backups"
    output_jsonl = tmp_path / "scenario_injections.jsonl"
    _seed_sqlite(db_file)

    env = dict(
        **os.environ,
        MI_BASE_SEED="20",
        MI_SANDBOX_ROOT=str(sandbox_root),
        MI_APP_ROOT=str(app_root),
        MI_DB_FILE=str(db_file),
        MI_BACKUPS_ROOT=str(backups_root),
    )
    result = subprocess.run(
        ["bash", "scripts/v2_scenarios.sh", str(output_jsonl), "S01,S04"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert "injected scenario=S01" in result.stdout
    assert "injected scenario=S04" in result.stdout
    lines = [line for line in output_jsonl.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 2

    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["scenario_id"] == "S01"
    assert second["scenario_id"] == "S04"
