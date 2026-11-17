from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from mi_agent.config import MISettings
from mi_agent.errors import SecurityGateError
from mi_agent.tools.executor import ToolExecutor
from mi_agent.workflow.models import DynamicToolSpec
from tests.test_target_app_runtime import load_target_app_module


def _target_backed_tools(
    settings: MISettings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[ToolExecutor, Path, Path]:
    """A ToolExecutor whose HTTP calls go to the real target app (in-process)."""
    app_root = tmp_path / "target-app"
    (app_root / "data").mkdir(parents=True)
    (app_root / "schema.sql").write_text(
        Path("target/app/schema.sql").read_text(encoding="utf-8"), encoding="utf-8"
    )
    target = load_target_app_module(app_root)
    target.ensure_db()
    transport = httpx.ASGITransport(app=target.app)
    real_client = httpx.AsyncClient

    def _client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr("mi_agent.tools.executor.httpx.AsyncClient", _client)
    return ToolExecutor(settings), app_root / "backups", app_root / "data" / "app.db"


def _write_backup(backups: Path, epoch: int) -> None:
    backups.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(backups / f"app-{epoch}.db") as conn:
        conn.executescript(Path("target/app/schema.sql").read_text(encoding="utf-8"))


async def _run(tools: ToolExecutor, operation: str, params: dict[str, Any]) -> dict[str, Any]:
    tool_name = tools.register_temporary_dynamic_tool(
        "inc-a",
        DynamicToolSpec(name="t", rationale="r", operation=operation, params=params),
    )
    return await tools.execute_dynamic_tool("inc-a", tool_name)


@pytest.mark.asyncio
async def test_restore_goes_through_target_api(
    settings: MISettings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tools, backups, db_file = _target_backed_tools(settings, monkeypatch, tmp_path)
    _write_backup(backups, 1792854880)
    db_file.write_bytes(b"not-a-valid-sqlite-db")

    result = await _run(tools, "request_backup_restore", {"backup_id": "app-1792854880"})

    assert result == {"ok": True, "action": "request_backup_restore", "backup_id": "app-1792854880"}
    with sqlite3.connect(db_file) as conn:
        assert conn.execute("PRAGMA integrity_check;").fetchone() == ("ok",)


@pytest.mark.asyncio
async def test_monitor_health_pings_live_target_health_endpoint(
    settings: MISettings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tools, _backups, _db_file = _target_backed_tools(settings, monkeypatch, tmp_path)

    status = await tools.monitor_health()

    assert status.healthy is True
    assert status.detail == "healthy"
    assert status.payload["health"]["ok"] is True
    assert status.payload["health"]["status_code"] == 200


@pytest.mark.asyncio
async def test_restore_unknown_backup_reports_available_ids(
    settings: MISettings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tools, backups, _ = _target_backed_tools(settings, monkeypatch, tmp_path)
    _write_backup(backups, 1792854880)
    _write_backup(backups, 1792854890)

    result = await _run(tools, "request_backup_restore", {"backup_id": "latest_pre_fault"})

    assert result["ok"] is False
    assert result["error"] == (
        "backup_not_found: 'latest_pre_fault'; available: app-1792854890, app-1792854880"
    )


@pytest.mark.asyncio
async def test_diagnostics_include_backup_catalog(
    settings: MISettings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tools, backups, _ = _target_backed_tools(settings, monkeypatch, tmp_path)
    _write_backup(backups, 1792854880)

    diagnostics = await tools.collect_diagnostics()

    assert [row["backup_id"] for row in diagnostics["backups"]] == ["app-1792854880"]
    assert diagnostics["backups"][0]["created_at"] == "2026-10-24T15:14:40+00:00"


@pytest.mark.parametrize(
    "params",
    [{}, {"backup_id": "../app-1"}, {"backup_id": "app-1/restore"}, {"backup_id": ".."}],
)
def test_security_gate_rejects_restore_without_catalog_id(
    settings: MISettings, params: dict[str, Any]
) -> None:
    spec = DynamicToolSpec(
        name="restore", rationale="r", operation="request_backup_restore", params=params
    )
    with pytest.raises(ValidationError):
        ToolExecutor(settings).security_gate(spec)


def test_security_gate_rejects_run_sql_path_escape(settings: MISettings) -> None:
    spec = DynamicToolSpec(
        name="run_bad",
        rationale="r",
        operation="run_sql",
        params={"db_path": "../escape.db", "sql": "SELECT 1;"},
    )
    with pytest.raises(SecurityGateError, match="app root"):
        ToolExecutor(settings).security_gate(spec)


@pytest.mark.asyncio
async def test_security_gate_blocks_unknown_operation(settings) -> None:  # type: ignore[no-untyped-def]
    tools = ToolExecutor(settings)
    incident_id = "inc-b"
    tool_name = tools.register_temporary_dynamic_tool(
        incident_id,
        DynamicToolSpec(
            name="bad",
            rationale="bad",
            operation="drop_everything",
            params={},
        ),
    )

    with pytest.raises(SecurityGateError, match="operation not allowed"):
        await tools.execute_dynamic_tool(incident_id, tool_name)


@pytest.mark.asyncio
async def test_security_gate_blocks_path_escape(settings) -> None:  # type: ignore[no-untyped-def]
    tools = ToolExecutor(settings)
    source = settings.sandbox_root / "safe.db"
    source.write_text("backup", encoding="utf-8")

    incident_id = "inc-c"
    tool_name = tools.register_temporary_dynamic_tool(
        incident_id,
        DynamicToolSpec(
            name="copy_bad",
            rationale="copy",
            operation="replace_file",
            params={"source_path": "safe.db", "destination_path": "../escape.db"},
        ),
    )

    with pytest.raises(SecurityGateError, match="app root"):
        await tools.execute_dynamic_tool(incident_id, tool_name)


@pytest.mark.asyncio
async def test_monitor_health_detects_new_error_logs(settings) -> None:  # type: ignore[no-untyped-def]
    tools = ToolExecutor(settings)

    async def fake_get_json(path: str) -> dict[str, object]:
        if path == "/health/deep":
            return {"ok": True, "status_code": 200}
        if path == "/ops/logs?lines=200":
            return {
                "ok": True,
                "lines": [json.dumps({"level": "error", "message": "db missing"})],
            }
        return {"ok": True}

    tools._get_json = fake_get_json  # type: ignore[method-assign]

    first = await tools.monitor_health()
    second = await tools.monitor_health()

    assert first.healthy is True
    assert first.detail == "error_log_detected"
    assert first.payload.get("new_log_errors")
    assert second.healthy is True


@pytest.mark.asyncio
async def test_collect_diagnostics_prefers_log_endpoint(settings) -> None:  # type: ignore[no-untyped-def]
    tools = ToolExecutor(settings)

    async def fake_get_json(path: str) -> dict[str, object]:
        if path == "/health/deep":
            return {"ok": True, "status_code": 200}
        if path == "/ops/db/status":
            return {"ok": True, "status_code": 200, "exists": True}
        if path == "/ops/logs?lines=300":
            return {"ok": True, "lines": ["line-a", "line-b"]}
        return {"ok": True}

    tools._get_json = fake_get_json  # type: ignore[method-assign]

    diagnostics = await tools.collect_diagnostics()
    assert diagnostics["log_tail"] == ["line-a", "line-b"]


@pytest.mark.asyncio
async def test_collect_diagnostics_keeps_endpoint_as_source_when_endpoint_is_healthy_but_empty(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    tools = ToolExecutor(settings)
    settings.app_log.write_text("old-line-that-should-not-be-used\n", encoding="utf-8")

    async def fake_get_json(path: str) -> dict[str, object]:
        if path == "/health/deep":
            return {"ok": True, "status_code": 200}
        if path == "/ops/db/status":
            return {"ok": True, "status_code": 200, "exists": True}
        if path == "/ops/logs?lines=300":
            return {"ok": True, "status_code": 200, "lines": []}
        return {"ok": True}

    tools._get_json = fake_get_json  # type: ignore[method-assign]

    diagnostics = await tools.collect_diagnostics()

    assert diagnostics["log_tail"] == []
    assert diagnostics["log_collection"]["source"] == "target_endpoint"
    assert diagnostics["log_collection"]["warnings"] == []


@pytest.mark.asyncio
async def test_collect_diagnostics_reports_log_source_unavailable_when_endpoint_and_file_fail(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    tools = ToolExecutor(settings)

    async def fake_get_json(path: str) -> dict[str, object]:
        if path == "/health/deep":
            return {"ok": False, "status_code": 503, "error": "health_unreachable"}
        if path == "/ops/db/status":
            return {"ok": False, "status_code": 503, "error": "db_status_unreachable"}
        if path == "/ops/logs?lines=300":
            return {"ok": False, "status_code": 503, "error": "log_endpoint_timeout"}
        return {"ok": True}

    tools._get_json = fake_get_json  # type: ignore[method-assign]

    diagnostics = await tools.collect_diagnostics()

    assert diagnostics["log_tail"] == []
    assert diagnostics["log_collection"]["source"] == "unavailable"
    warnings = diagnostics["log_collection"]["warnings"]
    assert isinstance(warnings, list)
    assert warnings and warnings[0].startswith("log_endpoint_unavailable:")


@pytest.mark.parametrize("fault", ["drop_widgets_table", "delete_db", "corrupt_db"])
@pytest.mark.asyncio
async def test_diagnostics_after_a_simulated_fault_carry_symptoms_but_no_trace_of_the_fault(
    settings: MISettings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fault: str
) -> None:
    """A simulated fault must look like a real outage: MI sees the broken database through
    the target's normal endpoints, and nothing it reads names the fault or the test hook."""
    tools, _backups, _db_file = _target_backed_tools(settings, monkeypatch, tmp_path)

    injected = await tools._post_json(f"/ops/fault/{fault}")
    assert injected["ok"] is True

    diagnostics = await tools.collect_diagnostics()

    assert diagnostics["log_collection"]["source"] == "target_endpoint"
    assert diagnostics["runtime_paths"]["app_root"] == settings.app_root.as_posix()
    assert diagnostics["runtime_paths"]["sandbox_root"] == settings.sandbox_root.as_posix()
    assert diagnostics["health_deep"]["ok"] is False
    assert any("health deep failed" in line for line in diagnostics["log_tail"])
    blob = json.dumps(diagnostics).lower()
    for marker in (fault, "/ops/fault", "inject", "scenario"):
        assert marker not in blob


def test_collect_diagnostics_includes_the_app_schema(settings: MISettings) -> None:
    settings.schema_file.parent.mkdir(parents=True, exist_ok=True)
    settings.schema_file.write_text("CREATE TABLE widgets (id INTEGER);\n", encoding="utf-8")

    schema = ToolExecutor(settings).read_app_schema()

    assert schema["exists"] is True
    assert schema["sql"] == "CREATE TABLE widgets (id INTEGER);\n"

@pytest.mark.asyncio
async def test_retrieve_rag_chunks_catches_errors(settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tools = ToolExecutor(settings)

    def _boom(*args: object, **kwargs: object) -> list[dict[str, object]]:
        del args, kwargs
        raise RuntimeError("pinecone timeout")

    monkeypatch.setattr("mi_agent.tools.executor.search_sqlite_docs", _boom)

    rows = await tools.retrieve_rag_chunks("query", k=3)
    meta = tools.consume_last_rag_meta()

    assert rows == []
    assert meta["status"] == "error"
    assert meta["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_retrieve_rag_chunks_marks_missing_config_skipped(settings) -> None:  # type: ignore[no-untyped-def]
    tools = ToolExecutor(settings)
    tools._settings.pinecone_api_key = None

    rows = await tools.retrieve_rag_chunks("query", k=3)
    meta = tools.consume_last_rag_meta()

    assert rows == []
    assert meta["status"] == "skipped"
    assert meta["reason"] == "missing_config"
