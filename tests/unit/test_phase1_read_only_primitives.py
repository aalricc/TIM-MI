from __future__ import annotations

import sqlite3
import time
from typing import Any

import pytest

from mi_agent.errors import ToolParameterValidationError
from mi_agent.events.bus import EventBus
from mi_agent.tools.executor import ToolExecutor

pytestmark = pytest.mark.phase1


@pytest.mark.asyncio()
async def test_read_only_primitives_are_registered_with_metadata(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())
    contracts = tools.primitive_parameter_contracts()

    required_read_only = {
        "check_db_integrity",
        "probe_db_rw",
        "inspect_runtime_config",
        "health_deep_probe",
        "inspect_paths",
        "list_backups",
        "wait",
    }
    assert required_read_only.issubset(contracts.keys())

    for primitive_id in required_read_only:
        contract = contracts[primitive_id]
        assert contract["mutating"] is False
        assert contract["reversible"] is False
        assert contract["risk_tier"] == "read_only"
        description = contract["description"]
        assert isinstance(description, str)
        assert description.strip() != ""


@pytest.mark.asyncio()
async def test_check_db_integrity_reports_ok_for_healthy_db(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())

    result = await tools.execute("check_db_integrity", {})

    assert result.data["ok"] is True
    assert result.data["result_lines"] == ["ok"]


@pytest.mark.asyncio()
async def test_probe_db_rw_reports_write_read_and_rollback(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())

    result = await tools.execute("probe_db_rw", {})

    assert result.data["db_exists"] is True
    assert result.data["read_ok"] is True
    assert result.data["write_ok"] is True
    assert result.data["rolled_back"] is True


@pytest.mark.asyncio()
async def test_inspect_runtime_config_hides_backup_path(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())

    result = await tools.execute("inspect_runtime_config", {})

    config = result.data["config"]
    serialized = str(config)
    assert "backups_root" not in config
    assert test_settings.backups_root.as_posix() not in serialized
    assert config["paths"]["db_file"].endswith("app.db")


@pytest.mark.asyncio()
async def test_health_deep_probe_parses_fields(monkeypatch, test_settings) -> None:
    class _FakeResponse:
        def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict[str, Any]:
            return self._payload

    class _FakeClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
            return None

        async def get(self, url: str) -> _FakeResponse:
            assert url.endswith("/health/deep")
            return _FakeResponse(200, {"ok": True, "checks": {"db": "ok"}})

    import mi_agent.tools.executor as executor_module

    monkeypatch.setattr(executor_module.httpx, "AsyncClient", _FakeClient)
    tools = ToolExecutor(test_settings, EventBus())

    result = await tools.execute("health_deep_probe", {})

    assert result.data["status_code"] == 200
    assert result.data["ok"] is True
    assert result.data["fields"]["checks"]["db"] == "ok"


@pytest.mark.asyncio()
async def test_inspect_paths_reports_link_target_and_missing_entries(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())

    link = test_settings.app_root / "data" / "db_link"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(test_settings.db_file)

    result = await tools.execute(
        "inspect_paths",
        {"paths": ["data/app.db", "data/db_link", "data/does_not_exist"]},
    )

    by_input = {entry["input_path"]: entry for entry in result.data["paths"]}
    assert by_input["data/app.db"]["exists"] is True
    assert by_input["data/app.db"]["type"] == "file"
    assert by_input["data/db_link"]["type"] == "symlink"
    assert by_input["data/db_link"]["link_target"] is not None
    assert by_input["data/does_not_exist"]["exists"] is False


@pytest.mark.asyncio()
async def test_list_backups_returns_metadata_without_paths(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())
    test_settings.backups_root.mkdir(parents=True, exist_ok=True)

    valid = test_settings.backups_root / "20261007T000000Z.db"
    with sqlite3.connect(valid) as conn:
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t(v) VALUES ('ok')")
        conn.commit()

    invalid = test_settings.backups_root / "20261006T000000Z.db"
    invalid.write_bytes(b"not-a-sqlite-db")

    result = await tools.execute("list_backups", {})
    backups = result.data["backups"]
    assert len(backups) >= 2

    ids = {backup["backup_id"] for backup in backups}
    assert "20261007T000000Z" in ids
    assert "20261006T000000Z" in ids

    for backup in backups:
        assert "path" not in backup
        assert set(backup) == {"backup_id", "timestamp", "size_bytes", "verified"}


@pytest.mark.asyncio()
async def test_wait_pauses_within_bounds(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())

    started = time.perf_counter()
    result = await tools.execute("wait", {"seconds": 0.02})
    elapsed = time.perf_counter() - started

    assert result.data["waited_seconds"] == pytest.approx(0.02, abs=0.01)
    assert elapsed >= 0.01


@pytest.mark.asyncio()
async def test_wait_rejects_out_of_bounds_values(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())
    with pytest.raises(ToolParameterValidationError):
        await tools.execute("wait", {"seconds": 120.0})
