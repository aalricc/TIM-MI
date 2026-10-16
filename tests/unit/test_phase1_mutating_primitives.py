from __future__ import annotations

import sqlite3
from stat import S_IMODE

import pytest

from mi_agent.errors import ToolExecutionError
from mi_agent.events.bus import EventBus
from mi_agent.tools.executor import ToolExecutor

pytestmark = pytest.mark.phase1


@pytest.mark.asyncio()
async def test_new_mutating_primitives_are_registered_with_expected_tiers(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())
    contracts = tools.primitive_parameter_contracts()

    assert contracts["reindex_db"]["mutating"] is True
    assert contracts["reindex_db"]["reversible"] is True
    assert contracts["reindex_db"]["risk_tier"] == "reversible"

    assert contracts["checkpoint_wal"]["mutating"] is True
    assert contracts["checkpoint_wal"]["reversible"] is True
    assert contracts["checkpoint_wal"]["risk_tier"] == "reversible"

    assert contracts["recover_db_to_new_file"]["mutating"] is True
    assert contracts["recover_db_to_new_file"]["reversible"] is True
    assert contracts["recover_db_to_new_file"]["risk_tier"] == "reversible"

    assert contracts["move_path"]["mutating"] is True
    assert contracts["move_path"]["reversible"] is True
    assert contracts["move_path"]["risk_tier"] == "reversible"

    assert contracts["write_app_file"]["mutating"] is True
    assert contracts["write_app_file"]["reversible"] is True
    assert contracts["write_app_file"]["risk_tier"] == "reversible"

    assert contracts["remove_path"]["mutating"] is True
    assert contracts["remove_path"]["reversible"] is False
    assert contracts["remove_path"]["risk_tier"] == "irreversible"


@pytest.mark.asyncio()
async def test_recover_db_to_new_file_rolls_back_created_output(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())
    output_rel = "data/recovered.db"

    result = await tools.execute(
        "recover_db_to_new_file",
        {"source_db_path": "data/app.db", "output_path": output_rel},
    )
    assert result.snapshot is not None

    recovered = test_settings.app_root / output_rel
    assert recovered.exists()

    await tools.rollback(result.snapshot)
    assert not recovered.exists()


@pytest.mark.asyncio()
async def test_move_path_with_overwrite_is_reversible_by_snapshot(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())
    source = test_settings.app_root / "data" / "src.txt"
    destination = test_settings.app_root / "data" / "dst.txt"
    source.write_text("source-v1", encoding="utf-8")
    destination.write_text("dest-v1", encoding="utf-8")

    result = await tools.execute(
        "move_path",
        {
            "source_path": "data/src.txt",
            "destination_path": "data/dst.txt",
            "overwrite": True,
        },
    )
    assert result.snapshot is not None
    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "source-v1"

    await tools.rollback(result.snapshot)
    assert source.read_text(encoding="utf-8") == "source-v1"
    assert destination.read_text(encoding="utf-8") == "dest-v1"


@pytest.mark.asyncio()
async def test_write_app_file_is_reversible_by_snapshot(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())
    app_file = test_settings.app_root / "config.txt"
    app_file.write_text("before", encoding="utf-8")

    result = await tools.execute(
        "write_app_file",
        {"path": "config.txt", "content": "after"},
    )
    assert result.snapshot is not None
    assert app_file.read_text(encoding="utf-8") == "after"

    await tools.rollback(result.snapshot)
    assert app_file.read_text(encoding="utf-8") == "before"


@pytest.mark.asyncio()
async def test_remove_path_is_irreversible_tier(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())
    doomed = test_settings.app_root / "data" / "doomed.txt"
    doomed.write_text("bye", encoding="utf-8")

    result = await tools.execute("remove_path", {"path": "data/doomed.txt"})
    assert result.snapshot is not None
    assert not doomed.exists()

    await tools.rollback(result.snapshot)
    assert not doomed.exists()


@pytest.mark.asyncio()
async def test_set_permissions_is_reversible_for_db_directory(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())
    db_dir = test_settings.db_file.parent
    original_mode = S_IMODE(db_dir.stat().st_mode)

    result = await tools.execute(
        "set_permissions",
        {"path": "data", "mode": 0o700},
    )
    assert result.snapshot is not None
    assert S_IMODE(db_dir.stat().st_mode) == 0o700

    await tools.rollback(result.snapshot)
    assert S_IMODE(db_dir.stat().st_mode) == original_mode


@pytest.mark.asyncio()
async def test_rollback_restores_db_backup_when_db_file_is_unwritable(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())
    db_file = test_settings.db_file
    target_file = test_settings.app_root / "config.txt"
    target_file.write_text("before", encoding="utf-8")

    result = await tools.execute(
        "write_app_file",
        {"path": "config.txt", "content": "after"},
    )
    assert result.snapshot is not None

    db_file.chmod(0)
    assert S_IMODE(db_file.stat().st_mode) == 0

    await tools.rollback(result.snapshot)

    assert target_file.read_text(encoding="utf-8") == "before"
    assert S_IMODE(db_file.stat().st_mode) != 0
    with sqlite3.connect(db_file) as conn:
        row = conn.execute("SELECT COUNT(*) FROM widgets").fetchone()
    assert row is not None


@pytest.mark.asyncio()
async def test_snapshot_blocks_non_permission_mutation_when_db_is_unreadable(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())
    db_file = test_settings.db_file
    target_file = test_settings.app_root / "config_snapshot_guard.txt"

    db_file.chmod(0)
    assert S_IMODE(db_file.stat().st_mode) == 0

    with pytest.raises(ToolExecutionError, match="DB snapshot failed"):
        await tools.execute(
            "write_app_file",
            {"path": target_file.name, "content": "ok"},
        )
    assert not target_file.exists()


@pytest.mark.asyncio()
async def test_set_permissions_can_proceed_when_db_is_unreadable(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())
    db_file = test_settings.db_file

    db_file.chmod(0)
    assert S_IMODE(db_file.stat().st_mode) == 0

    result = await tools.execute(
        "set_permissions",
        {"path": "data/app.db", "mode": 0o660},
    )
    assert result.snapshot is not None
    assert S_IMODE(db_file.stat().st_mode) == 0o660


@pytest.mark.asyncio()
async def test_reindex_and_checkpoint_run_on_sqlite_file(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())

    reindex = await tools.execute("reindex_db", {})
    assert reindex.data["reindexed"] is True

    with sqlite3.connect(test_settings.db_file) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT INTO widgets(name) VALUES ('checkpoint')")
        conn.commit()

    checkpoint = await tools.execute("checkpoint_wal", {})
    assert checkpoint.data["checkpointed"] in {True, False}
    assert isinstance(checkpoint.data["busy"], int)


@pytest.mark.asyncio()
async def test_reindex_sqlite_error_is_reported_as_tool_error(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())

    with sqlite3.connect(test_settings.db_file) as conn:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_widgets_name ON widgets(name)")
        conn.execute("PRAGMA writable_schema = ON")
        conn.execute(
            "UPDATE sqlite_master "
            "SET sql = 'CREATE INDEX idx_widgets_name ON widgets(nonexistent)' "
            "WHERE type='index' AND name='idx_widgets_name'"
        )
        conn.execute("PRAGMA writable_schema = OFF")
        conn.commit()

    with pytest.raises(ToolExecutionError, match="reindex_db failed"):
        await tools.execute("reindex_db", {})
