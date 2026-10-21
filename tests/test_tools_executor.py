from __future__ import annotations

import json
from pathlib import Path

import pytest

from mi_agent.errors import SecurityGateError
from mi_agent.tools.executor import ToolExecutor
from mi_agent.workflow.models import DynamicToolSpec


@pytest.mark.asyncio
async def test_restore_uses_latest_backup(settings) -> None:  # type: ignore[no-untyped-def]
    tools = ToolExecutor(settings)
    first = settings.backups_root / "catalog-old.db"
    second = settings.backups_root / "catalog-new.db"
    first.write_text("old", encoding="utf-8")
    second.write_text("new", encoding="utf-8")

    incident_id = "inc-a"
    tool_name = tools.register_temporary_dynamic_tool(
        incident_id,
        DynamicToolSpec(
            name="restore_latest",
            rationale="restore",
            operation="request_backup_restore",
            params={"backup_id": "latest"},
        ),
    )
    result = await tools.execute_dynamic_tool(incident_id, tool_name)

    assert result["ok"] is True
    assert Path(result["destination"]).exists()


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
