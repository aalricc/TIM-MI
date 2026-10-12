from __future__ import annotations

import sqlite3

import pytest

from mi_agent.errors import ToolParameterValidationError
from mi_agent.runtime import build_agent

pytestmark = pytest.mark.phase1


def _write_db(path, value: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS widgets(id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("DELETE FROM widgets")
        conn.execute("INSERT INTO widgets(name) VALUES (?)", (value,))
        conn.commit()


def _read_widget(path) -> str:
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT name FROM widgets ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    return str(row[0])


@pytest.mark.asyncio()
async def test_brokered_restore_swaps_db_and_quarantines_previous_copy(test_settings) -> None:
    _write_db(test_settings.db_file, "current")

    test_settings.backups_root.mkdir(parents=True, exist_ok=True)
    backup = test_settings.backups_root / "bk_001.db"
    _write_db(backup, "backup")

    _agent, context = build_agent(test_settings)

    response = await context.supervisor.request_backup_restore("bk_001")
    assert response["restored"] is True
    assert response["backup_id"] == "bk_001"
    assert response["verified"] is True

    assert _read_widget(test_settings.db_file) == "backup"

    quarantine_dir = test_settings.backups_root / "quarantine"
    quarantined = list(quarantine_dir.glob("*.db"))
    assert quarantined


@pytest.mark.asyncio()
async def test_request_backup_restore_primitive_uses_id_only_and_hides_paths(test_settings) -> None:
    _write_db(test_settings.db_file, "current")

    test_settings.backups_root.mkdir(parents=True, exist_ok=True)
    backup = test_settings.backups_root / "bk_002.db"
    _write_db(backup, "backup")

    _agent, context = build_agent(test_settings)

    result = await context.tools.execute("request_backup_restore", {"backup_id": "bk_002"})

    assert result.data["restored"] is True
    assert result.data["backup_id"] == "bk_002"
    assert "backup_path" not in result.data
    assert test_settings.backups_root.as_posix() not in str(result.data)


@pytest.mark.asyncio()
async def test_request_backup_restore_rejects_invalid_identifiers(test_settings) -> None:
    _agent, context = build_agent(test_settings)

    with pytest.raises(ToolParameterValidationError):
        await context.tools.execute("request_backup_restore", {"backup_id": "../escape"})

