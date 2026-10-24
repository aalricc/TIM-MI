from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path("scripts/find_known_good_backup.py")
SCHEMA = Path("target/app/schema.sql").read_text(encoding="utf-8")


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("find_known_good_backup", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = _load()


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _write_log(path: Path, rows: list[tuple[int, str, str]]) -> None:
    # Same shape as target/app/main.py::write_log.
    lines = [
        json.dumps({"ts": _iso(ts), "level": level, "message": msg}) for ts, level, msg in rows
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _good_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO widgets(name) VALUES ('w1')")


def _dropped_table_db(path: Path) -> None:
    _good_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE widgets")


def _narrow_schema_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")


def _corrupt_db(path: Path) -> None:
    path.write_bytes(b"not-a-valid-sqlite-db")  # what POST /ops/fault/corrupt_db writes


@pytest.fixture
def stack(tmp_path: Path) -> tuple[Path, Path]:
    """Healthy until t=1000, fault at 1005; backups every 10 s around it."""
    backups = tmp_path / "backups"
    backups.mkdir()
    _good_db(backups / "app-970.db")
    _narrow_schema_db(backups / "app-980.db")  # passed /health/deep, breaks GET /widgets
    _good_db(backups / "app-990.db")
    _corrupt_db(backups / "app-1000.db")  # at cutoff, already broken
    _corrupt_db(backups / "app-1010.db")  # after the fault
    _good_db(backups / "app-1020.db")  # after cutoff: excluded even though valid
    (backups / "notes.txt").write_text("ignored", encoding="utf-8")
    log = tmp_path / "app.log"
    _write_log(
        log,
        [
            (990, "info", "heartbeat"),
            (1000, "info", "heartbeat"),
            (1005, "error", "fault injected"),
            (1006, "error", "heartbeat"),
            (1009, "error", "heartbeat"),
        ],
    )
    return log, backups


def test_picks_newest_valid_backup_at_or_before_last_healthy_heartbeat(
    stack: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    log, backups = stack

    code = tool.main(["--log", str(log), "--backups", str(backups)])

    out = capsys.readouterr()
    assert code == 0
    assert out.out.strip() == str(backups / "app-990.db")
    assert "skipped app-1000.db: unreadable" in out.err
    assert "app-1020.db" not in out.err  # newer than the cutoff: never considered


def test_rejects_dropped_table_and_missing_columns(tmp_path: Path) -> None:
    dropped = tmp_path / "app-1.db"
    narrow = tmp_path / "app-2.db"
    _dropped_table_db(dropped)
    _narrow_schema_db(narrow)

    assert tool.validate_backup(dropped) == "widgets table missing"
    assert tool.validate_backup(narrow) == "widgets missing columns: ['created_at']"


def test_before_overrides_heartbeat_cutoff(
    stack: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    log, backups = stack

    assert tool.main(["--log", str(log), "--backups", str(backups), "--before", "985"]) == 0
    assert capsys.readouterr().out.strip() == str(backups / "app-970.db")
    assert tool.main(["--backups", str(backups), "--before", _iso(1025)]) == 0
    assert capsys.readouterr().out.strip() == str(backups / "app-1020.db")


def test_exit_codes_when_no_cutoff_or_no_valid_backup(
    tmp_path: Path, stack: tuple[Path, Path]
) -> None:
    _, backups = stack
    unhealthy_log = tmp_path / "unhealthy.log"
    _write_log(unhealthy_log, [(1006, "error", "heartbeat")])

    assert tool.main(["--log", str(unhealthy_log), "--backups", str(backups)]) == 2
    assert tool.main(["--backups", str(backups), "--before", "969"]) == 1


def test_validation_never_modifies_backups(stack: tuple[Path, Path]) -> None:
    log, backups = stack
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in backups.iterdir()}

    tool.main(["--log", str(log), "--backups", str(backups)])

    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in backups.iterdir()}
    assert after == before


def test_check_mode_validates_a_single_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good, corrupt, narrow = tmp_path / "good.db", tmp_path / "bad.db", tmp_path / "narrow.db"
    _good_db(good)
    _corrupt_db(corrupt)
    _narrow_schema_db(narrow)

    assert tool.main(["--check", str(good)]) == 0
    assert tool.main(["--check", str(corrupt)]) == 1
    assert tool.main(["--check", str(narrow)]) == 1
    assert "widgets missing columns: ['created_at']" in capsys.readouterr().err
    assert tool.main(["--check", str(tmp_path / "absent.db")]) == 1


def test_runs_from_stdin_as_documented(stack: tuple[Path, Path]) -> None:
    # Runbooks pipe the script into the container: python - < scripts/find_known_good_backup.py
    log, backups = stack
    with SCRIPT.open("rb") as script:
        # Fixed interpreter and test-owned paths; no untrusted input.
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-", "--log", str(log), "--backups", str(backups)],
            stdin=script,
            capture_output=True,
            check=False,
        )
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode().strip() == str(backups / "app-990.db")
