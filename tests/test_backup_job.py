from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path("target/backup/backup.sh")
SCHEMA = Path("target/app/schema.sql").read_text(encoding="utf-8")
# The container's /bin/sh is dash (python:3.12-slim); also run under the local sh.
SHELLS = sorted({s for s in (shutil.which("sh"), shutil.which("dash")) if s is not None})
pytestmark = pytest.mark.parametrize("shell", SHELLS)


def _run(
    shell: str, db: Path, backups: Path, **env: str
) -> subprocess.CompletedProcess[str]:
    environ = {
        **os.environ,
        "BACKUP_ONCE": "1",
        "BACKUP_DB_FILE": str(db),
        "BACKUP_DIR": str(backups),
        "BACKUP_PYTHON": sys.executable,
        "BACKUP_VALIDATOR": "scripts/find_known_good_backup.py",
        **env,
    }
    # Fixed shell and repo script; no untrusted input.
    return subprocess.run(  # noqa: S603
        [shell, str(SCRIPT)], env=environ, capture_output=True, text=True, check=False
    )


def _backups(directory: Path) -> list[Path]:
    return sorted(directory.glob("app-*.db"))


def test_healthy_database_is_backed_up(shell: str, tmp_path: Path) -> None:
    db = tmp_path / "app.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(SCHEMA)

    result = _run(shell, db, tmp_path / "backups")

    assert result.returncode == 0, result.stderr
    [backup] = _backups(tmp_path / "backups")
    assert backup.read_bytes() == db.read_bytes()
    assert not list((tmp_path / "backups").glob(".*.tmp"))


def test_faulty_states_are_not_kept(shell: str, tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not-a-valid-sqlite-db")  # POST /ops/fault/corrupt_db
    dropped = tmp_path / "dropped.db"
    with sqlite3.connect(dropped) as conn:
        conn.executescript(SCHEMA)
        conn.execute("DROP TABLE widgets")  # POST /ops/fault/drop_widgets_table
    narrow = tmp_path / "narrow.db"
    with sqlite3.connect(narrow) as conn:
        conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")

    for db in (corrupt, dropped, narrow):
        backups = tmp_path / f"backups-{db.stem}"
        result = _run(shell, db, backups)
        assert result.returncode == 0
        assert _backups(backups) == [], db.name
        assert "backup skipped: database failed validation" in result.stderr
        assert list(backups.iterdir()) == []  # temp copy removed too


def test_missing_database_is_skipped(shell: str, tmp_path: Path) -> None:
    result = _run(shell, tmp_path / "absent.db", tmp_path / "backups")

    assert result.returncode == 0
    assert list((tmp_path / "backups").iterdir()) == []


def test_retention_keeps_newest_backups(shell: str, tmp_path: Path) -> None:
    db = tmp_path / "app.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(SCHEMA)
    backups = tmp_path / "backups"
    backups.mkdir()
    for index, epoch in enumerate((100, 200, 300, 400, 500)):
        old = backups / f"app-{epoch}.db"
        old.write_bytes(db.read_bytes())
        os.utime(old, (1_000 + index, 1_000 + index))

    result = _run(shell, db, backups, BACKUP_RETENTION="3")

    assert result.returncode == 0, result.stderr
    names = [p.name for p in _backups(backups)]
    assert len(names) == 3
    assert "app-400.db" in names and "app-500.db" in names  # newest pre-existing kept
    assert not {"app-100.db", "app-200.db", "app-300.db"} & set(names)
