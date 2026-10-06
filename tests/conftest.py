from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mi_agent.config import MISettings


@pytest.fixture()
def test_settings(tmp_path: Path) -> MISettings:
    sandbox_root = tmp_path / "target"
    app_root = sandbox_root / "app"
    backups_root = sandbox_root / "backups"
    data_dir = app_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_file = data_dir / "app.db"
    schema_file = app_root / "schema.sql"
    schema_file.write_text(
        "CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT NOT NULL);\n",
        encoding="utf-8",
    )
    app_log = app_root / "app.log"
    app_log.write_text("", encoding="utf-8")

    with sqlite3.connect(db_file) as conn:
        conn.executescript(schema_file.read_text(encoding="utf-8"))

    state_dir = tmp_path / "state"

    return MISettings(
        monitor_interval_seconds=0.05,
        max_attempts_per_incident=4,
        max_incident_seconds=20,
        supervisor_backoff_initial_seconds=0.01,
        supervisor_backoff_max_seconds=0.05,
        app_root=app_root,
        sandbox_root=sandbox_root,
        backups_root=backups_root,
        db_file=db_file,
        app_log=app_log,
        schema_file=schema_file,
        policy_store_path=state_dir / "policy_store.json",
        skill_store_path=state_dir / "skills.json",
        state_dir=state_dir,
        target_service_url="http://invalid.local",
    )
