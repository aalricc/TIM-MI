from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from mi_agent.agent.engine import RecoveryAgent
from mi_agent.api.server import create_api
from mi_agent.events import EventLog
from mi_agent.runtime import MIContext
from mi_agent.workflow.models import (
    Diagnosis,
    DynamicToolSpec,
    HowProposal,
    MonitorStatus,
    VerificationResult,
)


class APIFakeLLM:
    def __init__(self) -> None:
        self._trace_index = 0

    async def preflight(self) -> dict[str, str]:
        return {"model": "test", "endpoint": "test", "status": "ok"}

    async def diagnose_why(self, *, evidence: dict[str, Any], rag_chunks: list[Any]) -> Diagnosis:
        del evidence, rag_chunks
        return Diagnosis(
            failure_type="schema_invalid",
            hypotheses=["widgets table missing"],
            evidence=["no such table: widgets"],
        )

    async def propose_how(
        self,
        *,
        diagnosis: Diagnosis,
        rag_chunks: list[Any],
        prior_attempts: list[Any],
        latest_tool_error: str | None,
        state_fingerprint: str | None,
    ) -> HowProposal:
        del diagnosis, rag_chunks, prior_attempts, latest_tool_error, state_fingerprint
        return HowProposal(
            primary=DynamicToolSpec(
                name="restore_latest",
                rationale="recover with latest known good backup",
                operation="request_backup_restore",
                params={"backup_id": "latest"},
            ),
            alternatives=[
                DynamicToolSpec(
                    name="repair_schema",
                    rationale="recreate missing table",
                    operation="run_sql",
                    params={"db_path": "data/app.db", "sql": "CREATE TABLE widgets(id INTEGER);"},
                ),
                DynamicToolSpec(
                    name="fallback_restart",
                    rationale="restart as fallback only",
                    operation="restart_service",
                    params={"service_name": "target-app"},
                ),
            ],
        )

    def consume_trace(self) -> dict[str, Any]:
        self._trace_index += 1
        if self._trace_index == 1:
            return {
                "request": {
                    "openai_api_key": "sk-secret-key",
                    "Authorization": "Bearer abc123token",
                },
                "response": {"raw": "diagnose"},
            }
        return {
            "request": {"PINECONE_API_KEY": "pcsk-secret", "token": "other-secret"},
            "response": {"raw": "propose"},
        }


class APIFakeTools:
    def __init__(self) -> None:
        self.catalog: dict[str, dict[str, Any]] = {
            "winning_tool": {
                "name": "winning_tool",
                "rationale": "prior winner",
                "operation": "request_backup_restore",
                "params": {"backup_id": "latest"},
            }
        }

    async def monitor_health(self) -> MonitorStatus:
        return MonitorStatus(healthy=False, status_code=500, detail="error_log_detected")

    async def collect_diagnostics(self) -> dict[str, Any]:
        return {
            "health_deep": {"ok": False, "error": "schema_invalid"},
            "db_status": {"ok": False, "error": "schema_invalid"},
            "db_integrity": {"ok": True, "rows": ["ok"]},
            "log_tail": ["no such table: widgets"],
        }

    async def retrieve_rag_chunks(self, query: str, *, k: int) -> list[Any]:
        del query, k
        return []

    def register_temporary_dynamic_tool(self, incident_id: str, spec: DynamicToolSpec) -> str:
        del incident_id
        return spec.name

    async def execute_dynamic_tool(self, incident_id: str, tool_name: str) -> dict[str, Any]:
        del incident_id
        return {"ok": False, "tool": tool_name, "error": "backup_missing"}

    def persist_dynamic_tool(self, incident_id: str, tool_name: str) -> None:
        del incident_id, tool_name

    def discard_incident_tools(self, incident_id: str, *, keep: str | None = None) -> None:
        del incident_id, keep

    def state_fingerprint(self) -> str:
        return "fp-1"


class APIFakeVerifier:
    async def verify(self) -> VerificationResult:
        return VerificationResult(
            healthy=False,
            failure_type="schema_invalid",
            details={"ok": False},
        )


def _build_client(tmp_path: Path) -> TestClient:
    events = EventLog(tmp_path / "events.jsonl")
    llm = APIFakeLLM()
    tools = APIFakeTools()
    agent = RecoveryAgent(
        llm=llm,  # type: ignore[arg-type]
        tools=tools,  # type: ignore[arg-type]
        verifier=APIFakeVerifier(),  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=60.0,
    )
    asyncio.run(agent.run_iteration())
    agent._policy = {
        "restore_latest": {"attempts": 2.0, "reward_total": 1.0},
        "repair_schema": {"attempts": 1.0, "reward_total": -1.0},
    }

    async def _idle_forever() -> None:
        while True:
            await asyncio.sleep(3600)

    agent.run_forever = _idle_forever  # type: ignore[method-assign]

    from mi_agent.config import MISettings

    settings = MISettings(openai_api_key="test-key")
    context = MIContext(
        settings=settings,
        events=events,
        tools=tools,  # type: ignore[arg-type]
        verifier=APIFakeVerifier(),  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
    )
    app = create_api(context, agent)
    return TestClient(app)


def test_dashboard_route_serves_html(tmp_path: Path) -> None:
    with _build_client(tmp_path) as client:
        response = client.get("/dashboard")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "MI Dashboard" in response.text


def test_agent_dashboard_schema_and_rl_summary(tmp_path: Path) -> None:
    with _build_client(tmp_path) as client:
        response = client.get("/agent/dashboard")

    assert response.status_code == 200
    payload = response.json()
    assert payload["agent_state"]["workflow_state"] in {"incident_active", "healthy", "recovered"}
    assert isinstance(payload["timeline"], list)
    assert payload["rl_summary"]["actions"][0]["avg_reward"] == 0.5
    assert payload["latest_why"]["failure_type"] == "schema_invalid"
    assert payload["latest_how"]["executed_candidate"] == "primary"
    assert len(payload["latest_how"]["alternatives"]) == 2


def test_agent_llm_trace_redacts_secrets(tmp_path: Path) -> None:
    with _build_client(tmp_path) as client:
        response = client.get("/agent/llm-trace")
        assert response.status_code == 200
        payload = response.json()
        trace = payload["trace"]
        assert isinstance(trace, dict)
        raw = trace["raw"]
        assert raw["diagnose_why"]["request"]["openai_api_key"] == "[REDACTED]"
        assert raw["diagnose_why"]["request"]["Authorization"] == "[REDACTED]"
        assert raw["propose_how"]["request"]["PINECONE_API_KEY"] == "[REDACTED]"

        incident_id = trace["incident_id"]
        by_incident = client.get("/agent/llm-trace", params={"incident_id": incident_id})
        assert by_incident.status_code == 200
        assert by_incident.json()["trace"]["incident_id"] == incident_id
