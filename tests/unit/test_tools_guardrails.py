from __future__ import annotations

import sqlite3

import pytest

from mi_agent.errors import (
    BackupAccessDeniedError,
    PathScopeError,
    PrimitiveNotAllowedError,
    ToolParameterValidationError,
)
from mi_agent.events.bus import EventBus
from mi_agent.tools.executor import ToolExecutor


@pytest.mark.asyncio()
async def test_non_allowlisted_primitive_raises(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())
    with pytest.raises(PrimitiveNotAllowedError):
        await tools.execute("drop_all", {})


@pytest.mark.asyncio()
async def test_bad_parameters_raise_typed_error(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())
    with pytest.raises(ToolParameterValidationError):
        await tools.execute("read_app_log", {"lines": "bad"})


@pytest.mark.asyncio()
async def test_out_of_scope_path_raises(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())
    with pytest.raises(PathScopeError):
        await tools.execute("stat_path", {"path": "../../etc/passwd"})


@pytest.mark.asyncio()
async def test_backup_path_is_structurally_denied(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())
    test_settings.backups_root.mkdir(parents=True, exist_ok=True)

    with pytest.raises(BackupAccessDeniedError):
        await tools.execute("stat_path", {"path": "../backups"})

    with pytest.raises(BackupAccessDeniedError):
        await tools.execute("list_directory", {"path": "../backups"})


@pytest.mark.asyncio()
async def test_symlink_path_into_backups_is_denied(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())
    test_settings.backups_root.mkdir(parents=True, exist_ok=True)
    link_path = test_settings.app_root / "data" / "backup_link"
    if link_path.exists() or link_path.is_symlink():
        link_path.unlink()
    link_path.symlink_to(test_settings.backups_root)

    with pytest.raises(BackupAccessDeniedError):
        await tools.execute("list_directory", {"path": "data/backup_link"})


@pytest.mark.asyncio()
async def test_snapshot_taken_before_mutation_and_rollback_on_failure(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())

    with sqlite3.connect(test_settings.db_file) as conn:
        conn.execute("INSERT INTO widgets(name) VALUES ('before')")
        conn.commit()

    result = await tools.execute("create_empty_sqlite_db", {"path": "data/app.db"})
    assert result.snapshot is not None

    await tools.rollback(result.snapshot)

    with sqlite3.connect(test_settings.db_file) as conn:
        row = conn.execute("SELECT name FROM widgets ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert row[0] == "before"
