from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from mi_agent.agent.engine import RecoveryAgent
from mi_agent.api.server import create_api
from mi_agent.decision.client import JevDecision
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
    def __init__(
        self,
        *,
        execution_ok: bool = False,
        monitor_health_sequence: list[bool] | None = None,
    ) -> None:
        self.catalog: dict[str, dict[str, Any]] = {
            "winning_tool": {
                "name": "winning_tool",
                "rationale": "prior winner",
                "operation": "request_backup_restore",
                "params": {"backup_id": "latest"},
            }
        }
        self._execution_ok = execution_ok
        self._monitor_health_sequence = monitor_health_sequence or [False]
        self._monitor_index = 0

    async def monitor_health(self) -> MonitorStatus:
        idx = min(self._monitor_index, len(self._monitor_health_sequence) - 1)
        healthy = self._monitor_health_sequence[idx]
        self._monitor_index += 1
        if healthy:
            return MonitorStatus(healthy=True, status_code=200, detail="healthy")
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
        return {
            "ok": self._execution_ok,
            "tool": tool_name,
            "error": None if self._execution_ok else "backup_missing",
        }

    def persist_dynamic_tool(self, incident_id: str, tool_name: str) -> None:
        del incident_id, tool_name

    def discard_incident_tools(self, incident_id: str, *, keep: str | None = None) -> None:
        del incident_id, keep

    def state_fingerprint(self) -> str:
        return "fp-1"


class APIFakeVerifier:
    def __init__(self, *, healthy: bool = False) -> None:
        self._healthy = healthy

    async def verify(self) -> VerificationResult:
        if self._healthy:
            return VerificationResult(healthy=True, details={"ok": True})
        return VerificationResult(
            healthy=False,
            failure_type="schema_invalid",
            details={"ok": False},
        )


class APIFakeJev:
    async def choose_candidate(
        self,
        *,
        state: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> JevDecision:
        del state, candidates
        return JevDecision(
            usable=True,
            selected_candidate_name="restore_latest",
            selected_candidate_operation="request_backup_restore",
            usable_probability=0.91,
            risk_score=0.2,
        )


def _build_client(
    tmp_path: Path,
    *,
    execution_ok: bool = False,
    verifier_healthy: bool = False,
    monitor_health_sequence: list[bool] | None = None,
) -> tuple[TestClient, EventLog]:
    if "pinecone" not in sys.modules:
        pinecone_stub = types.ModuleType("pinecone")
        pinecone_stub.__version__ = "test"
        sys.modules["pinecone"] = pinecone_stub

    events = EventLog(tmp_path / "events.jsonl")
    llm = APIFakeLLM()
    tools = APIFakeTools(
        execution_ok=execution_ok,
        monitor_health_sequence=monitor_health_sequence,
    )
    verifier = APIFakeVerifier(healthy=verifier_healthy)
    agent = RecoveryAgent(
        llm=llm,  # type: ignore[arg-type]
        tools=tools,  # type: ignore[arg-type]
        verifier=verifier,  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=60.0,
        jev=APIFakeJev(),  # type: ignore[arg-type]
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

    settings = MISettings(
        openai_api_key="test-key",
        jev_api_key="test-jev-key",
        pinecone_api_key="test-pinecone-key",
        pinecone_index_host="test-index-host",
    )
    context = MIContext(
        settings=settings,
        events=events,
        tools=tools,  # type: ignore[arg-type]
        verifier=verifier,  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
    )
    app = create_api(context, agent)
    return TestClient(app), events


def test_dashboard_route_serves_html(tmp_path: Path) -> None:
    client, _ = _build_client(tmp_path)
    with client:
        response = client.get("/dashboard")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "MI Dashboard" in response.text


def test_agent_dashboard_schema_and_rl_summary(tmp_path: Path) -> None:
    client, _ = _build_client(tmp_path)
    with client:
        response = client.get("/agent/dashboard")

    assert response.status_code == 200
    payload = response.json()
    assert payload["agent_state"]["workflow_state"] in {"incident_active", "healthy", "recovered"}
    assert isinstance(payload["timeline"], list)
    assert isinstance(payload["history"], list)
    assert payload["rl_summary"]["actions"][0]["avg_reward"] == 0.5
    assert payload["latest_why"]["failure_type"] == "schema_invalid"
    assert payload["latest_how"]["executed_candidate"] == "primary"
    assert len(payload["latest_how"]["alternatives"]) == 2
    live_view = payload["live_incident_view"]
    assert isinstance(live_view, dict)
    assert live_view["incident_id"] == payload["agent_state"]["active_incident"]["incident_id"]


def test_agent_dashboard_timeline_prefers_workflow_step_events(tmp_path: Path) -> None:
    client, _ = _build_client(tmp_path)
    with client:
        response = client.get("/agent/dashboard")

    assert response.status_code == 200
    payload = response.json()
    timeline = payload["timeline"]
    assert isinstance(timeline, list)
    assert timeline
    assert all(row.get("event_type") == "workflow.step" for row in timeline)

    step_rows = [row.get("payload") for row in timeline if isinstance(row.get("payload"), dict)]
    step_names = {str(row.get("step")) for row in step_rows}
    assert "class_memory.lookup" in step_names
    assert "jev.requested" in step_names
    assert "jev.result" in step_names


def test_agent_dashboard_timeline_includes_post_success_persist_steps(tmp_path: Path) -> None:
    client, _ = _build_client(
        tmp_path,
        execution_ok=True,
        verifier_healthy=True,
        monitor_health_sequence=[False, False],
    )
    with client:
        response = client.get("/agent/dashboard")

    assert response.status_code == 200
    timeline = response.json()["timeline"]
    step_rows = [row.get("payload") for row in timeline if isinstance(row.get("payload"), dict)]
    step_names = {str(row.get("step")) for row in step_rows}
    assert "persist.tool_catalog" in step_names
    assert "persist.policy_store" in step_names
    assert "persist.skills" in step_names
    assert "persist.class_memory" in step_names
    assert "post_recovery.ping" in step_names


def test_startup_emits_rag_summary_event(tmp_path: Path) -> None:
    client, events = _build_client(tmp_path)
    with client:
        response = client.get("/agent/dashboard")

    assert response.status_code == 200
    rag_events = [item for item in events.events if item.event_type == "rag.startup"]
    assert len(rag_events) == 1
    payload = rag_events[0].payload
    assert payload["enabled"] is True
    assert payload["namespace"] == "sqlite-docs"
    assert payload["top_k"] == 5
    assert isinstance(payload.get("pinecone_version"), str)


def test_agent_llm_trace_redacts_secrets(tmp_path: Path) -> None:
    client, _ = _build_client(tmp_path)
    with client:
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


def test_live_timeline_filters_healthy_monitor_events(tmp_path: Path) -> None:
    client, events = _build_client(tmp_path)
    with client:
        asyncio.run(
            events.publish(
                "monitor.health",
                {"healthy": True, "detail": "healthy", "status_code": 200},
            )
        )
        response = client.get("/agent/dashboard")

    assert response.status_code == 200
    timeline = response.json()["timeline"]
    assert isinstance(timeline, list)
    assert not any(
        row.get("event_type") == "monitor.health"
        and isinstance(row.get("payload"), dict)
        and bool(row["payload"].get("healthy", False))
        for row in timeline
    )


def test_live_dashboard_clears_after_recovery_and_history_retains_record(tmp_path: Path) -> None:
    client, _ = _build_client(
        tmp_path,
        execution_ok=True,
        verifier_healthy=True,
        monitor_health_sequence=[False, True],
    )
    with client:
        response = client.get("/agent/dashboard")

    assert response.status_code == 200
    payload = response.json()
    assert payload["agent_state"]["workflow_state"] == "recovered"
    assert payload["agent_state"]["active_incident"] is None
    assert payload["timeline"] == []
    assert payload["latest_why"] is None
    assert payload["latest_how"] is None
    assert payload["live_incident_view"] is None

    history = payload["history"]
    assert isinstance(history, list)
    assert len(history) >= 1
    row = history[0]
    assert row["status"] == "recovered"
    assert isinstance(row["incident_id"], str)


def test_history_trace_lookup_by_incident_id(tmp_path: Path) -> None:
    client, _ = _build_client(
        tmp_path,
        execution_ok=True,
        verifier_healthy=True,
        monitor_health_sequence=[False, True],
    )
    with client:
        dashboard_response = client.get("/agent/dashboard")
        assert dashboard_response.status_code == 200
        history = dashboard_response.json()["history"]
        assert isinstance(history, list)
        assert len(history) >= 1
        incident_id = history[0]["incident_id"]

        trace_response = client.get("/agent/llm-trace", params={"incident_id": incident_id})
        assert trace_response.status_code == 200
        trace = trace_response.json()["trace"]
        assert isinstance(trace, dict)
        assert trace["incident_id"] == incident_id


def test_incident_views_limit_cards_to_six(tmp_path: Path) -> None:
    client, events = _build_client(tmp_path)
    incident_id = "inc-limit-1"

    with client:
        for idx in range(8):
            asyncio.run(events.publish("incident.started", {"incident_id": incident_id}))
            asyncio.run(
                events.publish(
                    "dynamic_tool.executed",
                    {
                        "incident_id": incident_id,
                        "tool_name": f"tool-{idx}",
                        "action_key": f"action-{idx}",
                        "action": {"operation": "run_sql"},
                    },
                )
            )
            asyncio.run(
                events.publish(
                    "verification.result",
                    {"incident_id": incident_id, "healthy": idx % 2 == 0},
                )
            )
        response = client.get("/agent/dashboard")

    assert response.status_code == 200
    payload = response.json()
    incident_view = payload["incident_views"][incident_id]
    assert len(incident_view["rl_summary"]["actions"]) == 6
    assert len(incident_view["tool_catalog"]) == 6
    assert all(row["incident_id"] == incident_id for row in incident_view["rl_summary"]["actions"])
    assert all(row["incident_id"] == incident_id for row in incident_view["tool_catalog"])


def test_history_incident_view_is_scoped_per_incident(tmp_path: Path) -> None:
    client, events = _build_client(tmp_path)
    alpha_id = "inc-history-alpha"
    beta_id = "inc-history-beta"

    with client:
        for incident_id, action_key in ((alpha_id, "alpha-action"), (beta_id, "beta-action")):
            asyncio.run(events.publish("incident.started", {"incident_id": incident_id}))
            asyncio.run(
                events.publish(
                    "dynamic_tool.executed",
                    {
                        "incident_id": incident_id,
                        "tool_name": f"{incident_id}-tool",
                        "action_key": action_key,
                        "action": {"operation": "request_backup_restore"},
                    },
                )
            )
            asyncio.run(
                events.publish(
                    "verification.result",
                    {"incident_id": incident_id, "healthy": True},
                )
            )
            asyncio.run(
                events.publish(
                    "incident.recovered",
                    {
                        "incident_id": incident_id,
                        "tool_name": f"{incident_id}-tool",
                        "attempts": 1,
                    },
                )
            )

        response = client.get("/agent/dashboard")

    assert response.status_code == 200
    payload = response.json()
    history_ids = {row["incident_id"] for row in payload["history"]}
    assert alpha_id in history_ids
    assert beta_id in history_ids

    alpha_view = payload["incident_views"][alpha_id]
    beta_view = payload["incident_views"][beta_id]
    assert {row["action"] for row in alpha_view["rl_summary"]["actions"]} == {"alpha-action"}
    assert {row["action"] for row in beta_view["rl_summary"]["actions"]} == {"beta-action"}
    assert {row["action"] for row in alpha_view["tool_catalog"]} == {"alpha-action"}
    assert {row["action"] for row in beta_view["tool_catalog"]} == {"beta-action"}

    alpha_detail = payload["history_incident_views"][alpha_id]
    beta_detail = payload["history_incident_views"][beta_id]
    assert alpha_detail["agent_state"]["active_incident"]["incident_id"] == alpha_id
    assert beta_detail["agent_state"]["active_incident"]["incident_id"] == beta_id
    assert "latest_why" in alpha_detail
    assert "latest_how" in beta_detail
    assert all(
        isinstance(row.get("payload"), dict)
        and row["payload"].get("incident_id") == alpha_id
        for row in alpha_detail["timeline"]
    )
