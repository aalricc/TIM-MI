from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mi_agent.agent.engine import RecoveryAgent
from mi_agent.decision.client import JevDecision
from mi_agent.errors import SecurityGateError
from mi_agent.events import EventLog
from mi_agent.rag_query import apply_query_prefix_and_cap
from mi_agent.tools.executor import ToolExecutor, base_tool_name
from mi_agent.workflow.models import (
    Diagnosis,
    DynamicToolSpec,
    HowProposal,
    IncidentContext,
    MonitorStatus,
    VerificationResult,
)


class FakeLLM:
    def __init__(self) -> None:
        self.last_propose_rag_chunks: list[Any] = []

    async def diagnose_why(self, *, evidence: dict[str, Any], rag_chunks: list[Any]) -> Diagnosis:
        del evidence, rag_chunks
        return Diagnosis(failure_type="db_corrupt", hypotheses=["db issue"], evidence=["probe"])

    async def propose_how(
        self,
        *,
        diagnosis: Diagnosis,
        rag_chunks: list[Any],
        prior_attempts: list[Any],
        latest_tool_error: str | None,
        state_fingerprint: str | None,
    ) -> HowProposal:
        del diagnosis, prior_attempts, latest_tool_error, state_fingerprint
        self.last_propose_rag_chunks = list(rag_chunks)
        return HowProposal(
            primary=DynamicToolSpec(
                name="restore",
                rationale="recover db",
                operation="request_backup_restore",
                params={"backup_id": "latest"},
            ),
            alternatives=[
                DynamicToolSpec(
                    name="repair_sql",
                    rationale="repair",
                    operation="run_sql",
                    params={"db_path": "data/app.db", "sql": "PRAGMA integrity_check;"},
                ),
                DynamicToolSpec(
                    name="service_restart",
                    rationale="fallback",
                    operation="restart_service",
                    params={"service_name": "target-app"},
                ),
            ],
        )

    def consume_trace(self) -> dict[str, Any]:
        return {
            "request": {"authorization": "Bearer test-token"},
            "response": {"raw": "ok"},
        }


class FakeTools:
    def __init__(
        self,
        *,
        unhealthy: bool,
        execution_ok: bool,
        monitor_health_sequence: list[bool] | None = None,
    ) -> None:
        self.unhealthy = unhealthy
        self.execution_ok = execution_ok
        self._monitor_health_sequence = monitor_health_sequence
        self._monitor_index = 0
        self.catalog: dict[str, dict[str, Any]] = {}
        self.executions = 0
        self.persisted: list[str] = []
        self.registered_tools: list[str] = []

    async def monitor_health(self) -> MonitorStatus:
        if self._monitor_health_sequence is not None:
            idx = min(self._monitor_index, len(self._monitor_health_sequence) - 1)
            healthy = self._monitor_health_sequence[idx]
            self._monitor_index += 1
            return MonitorStatus(
                healthy=healthy,
                status_code=200 if healthy else 500,
                detail="healthy" if healthy else "health_endpoint_unhealthy",
            )
        return MonitorStatus(healthy=not self.unhealthy, status_code=500 if self.unhealthy else 200)

    async def collect_diagnostics(self) -> dict[str, Any]:
        return {"health_deep": {"ok": False, "error": "db_corrupt"}, "log_tail": ["err"]}

    async def retrieve_rag_chunks(self, query: str, *, k: int) -> list[Any]:
        del query, k
        return []

    def security_gate(self, spec: DynamicToolSpec) -> None:
        del spec

    def register_temporary_dynamic_tool(self, incident_id: str, spec: DynamicToolSpec) -> str:
        del incident_id
        self.catalog[spec.name] = spec.model_dump()
        self.registered_tools.append(spec.name)
        return spec.name

    async def execute_dynamic_tool(self, incident_id: str, tool_name: str) -> dict[str, Any]:
        del incident_id
        self.executions += 1
        return {
            "ok": self.execution_ok,
            "tool": tool_name,
            "error": None if self.execution_ok else "boom",
        }

    def persist_dynamic_tool(self, incident_id: str, tool_name: str) -> None:
        del incident_id
        self.persisted.append(tool_name)

    def discard_incident_tools(self, incident_id: str, *, keep: str | None = None) -> None:
        del incident_id, keep

    def state_fingerprint(self) -> str:
        return "fingerprint"


class FakeVerifier:
    def __init__(self, healthy: bool) -> None:
        self.healthy = healthy

    async def verify(self) -> VerificationResult:
        return VerificationResult(
            healthy=self.healthy,
            failure_type=None if self.healthy else "still_bad",
        )


class FakeJev:
    def __init__(self, decision: JevDecision) -> None:
        self.decision = decision

    async def choose_candidate(
        self,
        *,
        state: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> JevDecision:
        del state, candidates
        return self.decision


def _primary_jev() -> FakeJev:
    return FakeJev(
        JevDecision(
            usable=True,
            selected_candidate_name="restore",
            selected_candidate_operation="request_backup_restore",
            usable_probability=0.9,
            risk_score=0.2,
        )
    )


@pytest.mark.asyncio
async def test_success_persists_winning_dynamic_tool(tmp_path: Path) -> None:
    tools = FakeTools(unhealthy=True, execution_ok=True)
    llm = FakeLLM()
    agent = RecoveryAgent(
        llm=llm,
        tools=tools,  # type: ignore[arg-type]
        verifier=FakeVerifier(healthy=True),  # type: ignore[arg-type]
        events=EventLog(tmp_path / "events.jsonl"),
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=_primary_jev(),  # type: ignore[arg-type]
    )

    await agent.run_iteration()

    assert tools.executions == 1
    assert tools.persisted == ["restore"]
    assert tools.registered_tools == ["restore"]

    skills_path = tmp_path / "skills.md"
    assert skills_path.exists()
    skills_text = skills_path.read_text(encoding="utf-8")
    assert "name: recovery-restore" in skills_text
    assert "## RL Knowledge" in skills_text
    assert "rl_reward_total: 1.00" in skills_text

    # Ensure learned skills are fed back into planning context after first success.
    await agent.run_iteration()
    memory_sources = [
        getattr(chunk, "source", None)
        for chunk in llm.last_propose_rag_chunks
    ]
    assert "skills.md" in memory_sources


@pytest.mark.asyncio
async def test_success_emits_ordered_persist_steps(tmp_path: Path) -> None:
    tools = FakeTools(
        unhealthy=True,
        execution_ok=True,
        monitor_health_sequence=[False, True],
    )
    events = EventLog(tmp_path / "events.jsonl")
    agent = RecoveryAgent(
        llm=FakeLLM(),
        tools=tools,  # type: ignore[arg-type]
        verifier=FakeVerifier(healthy=True),  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=_primary_jev(),  # type: ignore[arg-type]
    )

    await agent.run_iteration()

    step_rows = [
        item.payload
        for item in events.events
        if item.event_type == "workflow.step" and isinstance(item.payload, dict)
    ]
    step_names = [str(row.get("step")) for row in step_rows]

    expected_order = [
        "persist.tool_catalog",
        "persist.policy_store",
        "persist.skills",
        "persist.class_memory",
        "post_recovery.ping",
        "attempt.result",
    ]
    indices = [step_names.index(name) for name in expected_order]
    assert indices == sorted(indices)
    assert any(
        row.get("step") == "post_recovery.ping" and row.get("status") == "healthy"
        for row in step_rows
    )


@pytest.mark.asyncio
async def test_post_recovery_healthy_ping_closes_incident(tmp_path: Path) -> None:
    tools = FakeTools(
        unhealthy=True,
        execution_ok=True,
        monitor_health_sequence=[False, True, True],
    )
    events = EventLog(tmp_path / "events.jsonl")
    agent = RecoveryAgent(
        llm=FakeLLM(),
        tools=tools,  # type: ignore[arg-type]
        verifier=FakeVerifier(healthy=True),  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=_primary_jev(),  # type: ignore[arg-type]
    )

    await agent.run_iteration()
    await agent.run_iteration()

    assert tools.executions == 1
    assert agent.snapshot.incident is None
    closed_events = [item for item in events.events if item.event_type == "incident.closed"]
    assert closed_events
    attempt_rows = [
        item.payload
        for item in events.events
        if item.event_type == "workflow.step"
        and isinstance(item.payload, dict)
        and item.payload.get("step") == "attempt.result"
    ]
    assert attempt_rows
    assert all(int(row.get("attempt", 0)) == 1 for row in attempt_rows)


@pytest.mark.asyncio
async def test_post_recovery_unhealthy_ping_keeps_incident_open(tmp_path: Path) -> None:
    tools = FakeTools(
        unhealthy=True,
        execution_ok=True,
        monitor_health_sequence=[False, False, False],
    )
    events = EventLog(tmp_path / "events.jsonl")
    agent = RecoveryAgent(
        llm=FakeLLM(),
        tools=tools,  # type: ignore[arg-type]
        verifier=FakeVerifier(healthy=True),  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=_primary_jev(),  # type: ignore[arg-type]
    )

    await agent.run_iteration()
    first_snapshot = agent.snapshot
    assert first_snapshot.incident is not None
    assert first_snapshot.incident.attempts == 1

    await agent.run_iteration()
    second_snapshot = agent.snapshot
    assert second_snapshot.incident is not None
    assert second_snapshot.incident.attempts == 2
    assert tools.executions == 2

    closed_events = [item for item in events.events if item.event_type == "incident.closed"]
    assert not closed_events
    assert any(
        item.event_type == "workflow.step"
        and isinstance(item.payload, dict)
        and item.payload.get("step") == "post_recovery.ping"
        and item.payload.get("status") == "unhealthy"
        for item in events.events
    )


@pytest.mark.asyncio
async def test_failures_continue_without_no_viable_cap(tmp_path: Path) -> None:
    tools = FakeTools(unhealthy=True, execution_ok=False)
    agent = RecoveryAgent(
        llm=FakeLLM(),
        tools=tools,  # type: ignore[arg-type]
        verifier=FakeVerifier(healthy=False),  # type: ignore[arg-type]
        events=EventLog(tmp_path / "events.jsonl"),
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=_primary_jev(),  # type: ignore[arg-type]
    )

    for _ in range(4):
        await agent.run_iteration()

    assert tools.executions == 4
    snapshot = agent.snapshot
    assert snapshot.incident is not None
    assert snapshot.incident.attempts == 4


@pytest.mark.asyncio
async def test_timeline_links_why_and_how_to_same_incident(tmp_path: Path) -> None:
    tools = FakeTools(unhealthy=True, execution_ok=False)
    events = EventLog(tmp_path / "events.jsonl")
    agent = RecoveryAgent(
        llm=FakeLLM(),
        tools=tools,  # type: ignore[arg-type]
        verifier=FakeVerifier(healthy=False),  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=_primary_jev(),  # type: ignore[arg-type]
    )

    await agent.run_iteration()

    why_events = [item for item in events.events if item.event_type == "diagnosis.why"]
    how_events = [item for item in events.events if item.event_type == "planning.how"]
    assert len(why_events) == 1
    assert len(how_events) == 1
    assert why_events[0].payload["incident_id"] == how_events[0].payload["incident_id"]


@pytest.mark.asyncio
async def test_alternatives_recorded_but_one_action_executed(tmp_path: Path) -> None:
    tools = FakeTools(unhealthy=True, execution_ok=False)
    agent = RecoveryAgent(
        llm=FakeLLM(),
        tools=tools,  # type: ignore[arg-type]
        verifier=FakeVerifier(healthy=False),  # type: ignore[arg-type]
        events=EventLog(tmp_path / "events.jsonl"),
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=_primary_jev(),  # type: ignore[arg-type]
    )

    await agent.run_iteration()

    assert tools.executions == 1
    trace = agent.get_llm_trace()
    assert isinstance(trace, dict)
    how = trace.get("how")
    assert isinstance(how, dict)
    alternatives = how.get("alternatives")
    assert isinstance(alternatives, list)
    assert len(alternatives) == 2
    assert how.get("executed_candidate") == "primary"


@pytest.mark.asyncio
async def test_workflow_step_events_are_explicit_and_ordered_for_full_loop(tmp_path: Path) -> None:
    tools = FakeTools(unhealthy=True, execution_ok=False)
    events = EventLog(tmp_path / "events.jsonl")
    agent = RecoveryAgent(
        llm=FakeLLM(),
        tools=tools,  # type: ignore[arg-type]
        verifier=FakeVerifier(healthy=False),  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=_primary_jev(),  # type: ignore[arg-type]
    )

    await agent.run_iteration()

    workflow_events = [item for item in events.events if item.event_type == "workflow.step"]
    assert workflow_events

    step_rows = [item.payload for item in workflow_events]
    step_names = [str(row.get("step")) for row in step_rows]

    assert "monitor.unhealthy_detected" in step_names
    assert "diagnostics.collected" in step_names
    assert "rag.symptom" in step_names
    assert "llm.why" in step_names
    assert "class_memory.lookup" in step_names
    assert "path.selected" in step_names
    assert "rag.repair" in step_names
    assert "llm.how" in step_names
    assert "jev.requested" in step_names
    assert "jev.result" in step_names
    assert "dynamic_tool.registered" in step_names
    assert "dynamic_tool.executed" in step_names
    assert "verification.result" in step_names
    assert "attempt.result" in step_names

    class_memory_rows = [row for row in step_rows if row.get("step") == "class_memory.lookup"]
    assert class_memory_rows
    assert class_memory_rows[-1].get("status") == "disabled"

    path_rows = [row for row in step_rows if row.get("step") == "path.selected"]
    assert path_rows
    assert path_rows[-1].get("path_mode") == "full_loop"

    jev_rows = [row for row in step_rows if str(row.get("step", "")).startswith("jev.")]
    assert len(jev_rows) >= 2
    assert all(int(row.get("attempt", 0)) == 1 for row in jev_rows)


@pytest.mark.asyncio
async def test_jev_unusable_skips_execution_and_loops_with_feedback(tmp_path: Path) -> None:
    tools = FakeTools(unhealthy=True, execution_ok=True)
    events = EventLog(tmp_path / "events.jsonl")
    agent = RecoveryAgent(
        llm=FakeLLM(),
        tools=tools,  # type: ignore[arg-type]
        verifier=FakeVerifier(healthy=True),  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=FakeJev(JevDecision(usable=False, reason="timeout")),  # type: ignore[arg-type]
    )

    await agent.run_iteration()

    assert tools.executions == 0
    snapshot = agent.snapshot
    assert snapshot.incident is not None
    assert snapshot.incident.attempts == 1
    assert snapshot.incident.last_tool_error == "timeout"
    assert snapshot.incident.prior_attempts[-1].tool_name == "decision_unusable"
    policy = agent.policy
    assert policy["decision_unusable"]["attempts"] == 1.0
    assert policy["decision_unusable"]["reward_total"] == -1.0

    decision_events = [item for item in events.events if item.event_type == "decision.unusable"]
    assert len(decision_events) == 1
    assert decision_events[0].payload["reason"] == "timeout"


@pytest.mark.asyncio
async def test_jev_request_error_unusable_does_not_penalize_policy(tmp_path: Path) -> None:
    tools = FakeTools(unhealthy=True, execution_ok=True)
    agent = RecoveryAgent(
        llm=FakeLLM(),
        tools=tools,  # type: ignore[arg-type]
        verifier=FakeVerifier(healthy=True),  # type: ignore[arg-type]
        events=EventLog(tmp_path / "events.jsonl"),
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=FakeJev(
            JevDecision(
                usable=False,
                reason="jev_request_error:TypeSafeAPITimeoutError",
            )
        ),  # type: ignore[arg-type]
    )

    await agent.run_iteration()

    assert tools.executions == 0
    assert "decision_unusable" not in agent.policy


def test_decision_payload_includes_shadow() -> None:
    payload = RecoveryAgent._decision_payload(
        JevDecision(
            usable=True,
            selected_candidate_name="repair_sql",
            selected_candidate_operation="run_sql",
            shadow={"selected_option": "option_1"},
        )
    )

    assert payload["shadow"] == {"selected_option": "option_1"}


@pytest.mark.asyncio
async def test_jev_can_select_alternative_action(tmp_path: Path) -> None:
    tools = FakeTools(unhealthy=True, execution_ok=True)
    agent = RecoveryAgent(
        llm=FakeLLM(),
        tools=tools,  # type: ignore[arg-type]
        verifier=FakeVerifier(healthy=True),  # type: ignore[arg-type]
        events=EventLog(tmp_path / "events.jsonl"),
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=FakeJev(
            JevDecision(
                usable=True,
                selected_candidate_name="repair_sql",
                selected_candidate_operation="run_sql",
                usable_probability=0.91,
                risk_score=0.2,
            )
        ),  # type: ignore[arg-type]
    )

    await agent.run_iteration()

    assert tools.executions == 1
    assert tools.persisted == ["repair_sql"]
    trace = agent.get_llm_trace()
    assert isinstance(trace, dict)
    how = trace.get("how")
    assert isinstance(how, dict)
    assert how.get("executed_candidate") == "alternative:repair_sql"


@pytest.mark.asyncio
async def test_rag_status_events_and_trace_are_recorded(tmp_path: Path) -> None:
    tools = FakeTools(unhealthy=True, execution_ok=False)
    events = EventLog(tmp_path / "events.jsonl")
    agent = RecoveryAgent(
        llm=FakeLLM(),
        tools=tools,  # type: ignore[arg-type]
        verifier=FakeVerifier(healthy=False),  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=_primary_jev(),  # type: ignore[arg-type]
    )

    await agent.run_iteration()

    rag_call_events = [item for item in events.events if item.event_type == "rag.call"]
    rag_empty_events = [item for item in events.events if item.event_type == "rag.empty"]
    assert len(rag_call_events) == 2
    assert len(rag_empty_events) == 2

    phases = {str(item.payload.get("phase")) for item in rag_call_events}
    assert phases == {"symptom", "repair"}

    trace = agent.get_llm_trace()
    assert isinstance(trace, dict)
    rag = trace.get("rag")
    assert isinstance(rag, dict)
    symptom = rag.get("symptom")
    repair = rag.get("repair")
    assert isinstance(symptom, dict)
    assert isinstance(repair, dict)
    assert symptom.get("status") == "empty"
    assert repair.get("status") == "empty"


def test_rag_query_rewrite_off_matches_legacy_prefix_behavior(tmp_path: Path) -> None:
    diagnostics = {"health_deep": {"error": "service timeout while polling"}}
    agent = RecoveryAgent(
        llm=FakeLLM(),
        tools=FakeTools(unhealthy=True, execution_ok=False),  # type: ignore[arg-type]
        verifier=FakeVerifier(healthy=False),  # type: ignore[arg-type]
        events=EventLog(tmp_path / "events.jsonl"),
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=_primary_jev(),  # type: ignore[arg-type]
        rag_query_prefix="sqlite",
        rag_query_max_chars=300,
        rag_query_rewrite=False,
    )

    expected = apply_query_prefix_and_cap(
        query="service timeout while polling",
        prefix="sqlite",
        max_chars=300,
    )
    assert agent._build_symptom_query(diagnostics) == expected


@pytest.mark.asyncio
async def test_rag_query_rewrite_on_uses_fetch_depth_per_retrieval_call(tmp_path: Path) -> None:
    class DepthTools:
        def __init__(self) -> None:
            self.calls: list[int] = []

        async def retrieve_rag_chunks(self, query: str, *, k: int) -> list[Any]:
            del query
            self.calls.append(k)
            return []

    tools = DepthTools()
    agent = RecoveryAgent(
        llm=FakeLLM(),
        tools=tools,  # type: ignore[arg-type]
        verifier=FakeVerifier(healthy=False),  # type: ignore[arg-type]
        events=EventLog(tmp_path / "events.jsonl"),
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=_primary_jev(),  # type: ignore[arg-type]
        rag_top_k=3,
        rag_fetch_depth=7,
        rag_query_rewrite=True,
    )

    incident = IncidentContext(incident_id="inc-rag-depth")
    await agent._retrieve_rag_chunks_for_incident(
        incident,
        phase="symptom",
        query="database disk image is malformed",
    )

    assert tools.calls == [7]


@pytest.mark.asyncio
async def test_rag_query_rewrite_off_uses_top_k_for_retrieval(tmp_path: Path) -> None:
    class DepthTools:
        def __init__(self) -> None:
            self.calls: list[int] = []

        async def retrieve_rag_chunks(self, query: str, *, k: int) -> list[Any]:
            del query
            self.calls.append(k)
            return []

    tools = DepthTools()
    agent = RecoveryAgent(
        llm=FakeLLM(),
        tools=tools,  # type: ignore[arg-type]
        verifier=FakeVerifier(healthy=False),  # type: ignore[arg-type]
        events=EventLog(tmp_path / "events.jsonl"),
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=_primary_jev(),  # type: ignore[arg-type]
        rag_top_k=3,
        rag_fetch_depth=7,
        rag_query_rewrite=False,
    )

    incident = IncidentContext(incident_id="inc-rag-topk")
    await agent._retrieve_rag_chunks_for_incident(
        incident,
        phase="symptom",
        query="database disk image is malformed",
    )

    assert tools.calls == [3]


class RejectingGateTools(FakeTools):
    def security_gate(self, spec: DynamicToolSpec) -> None:
        raise SecurityGateError(f"operation not allowed by security gate: {spec.operation}")


def _steps(events: EventLog) -> list[dict[str, Any]]:
    return [item.payload for item in events.events if item.event_type == "workflow.step"]


@pytest.mark.asyncio
async def test_security_gate_step_runs_before_registration(tmp_path: Path) -> None:
    tools = FakeTools(unhealthy=True, execution_ok=True, monitor_health_sequence=[False, True])
    events = EventLog(tmp_path / "events.jsonl")
    agent = RecoveryAgent(
        llm=FakeLLM(),
        tools=tools,  # type: ignore[arg-type]
        verifier=FakeVerifier(healthy=True),  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=_primary_jev(),  # type: ignore[arg-type]
    )

    await agent.run_iteration()

    names = [str(row.get("step")) for row in _steps(events)]
    gate = names.index("security.gate")
    assert names[gate - 1] == "jev.result"
    assert names[gate + 1] == "dynamic_tool.registered"
    assert _steps(events)[gate]["status"] == "ok"


@pytest.mark.asyncio
async def test_security_gate_rejection_is_a_failed_attempt(tmp_path: Path) -> None:
    tools = RejectingGateTools(unhealthy=True, execution_ok=True)
    events = EventLog(tmp_path / "events.jsonl")
    agent = RecoveryAgent(
        llm=FakeLLM(),
        tools=tools,  # type: ignore[arg-type]
        verifier=FakeVerifier(healthy=True),  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=_primary_jev(),  # type: ignore[arg-type]
    )

    await agent.run_iteration()

    assert tools.registered_tools == []
    assert tools.executions == 0
    tail = [(r["step"], r["status"]) for r in _steps(events)][-4:]
    assert tail == [
        ("jev.result", "usable"),
        ("security.gate", "rejected"),
        ("persist.policy_store", "failure"),
        ("attempt.result", "failed"),
    ]
    snapshot = agent.snapshot
    assert snapshot.incident is not None
    assert snapshot.incident.attempts == 1
    assert snapshot.incident.last_tool_error is not None
    assert snapshot.incident.last_tool_error.startswith("security_gate_rejected:SecurityGateError")
    assert snapshot.incident.prior_attempts[-1].tool_name == "restore"
    assert agent.policy["restore"] == {"attempts": 1.0, "reward_total": -1.0}
    assert not [item for item in events.events if item.event_type == "agent.loop_error"]

    # Next iteration retries from step 5 with the rejection fed back to HOW.
    await agent.run_iteration()
    retry_steps = [r for r in _steps(events) if r["attempt"] == 2]
    assert [r["step"] for r in retry_steps[:2]] == ["context.reused", "class_memory.lookup"]


class ExplodingGateTools(FakeTools):
    def security_gate(self, spec: DynamicToolSpec) -> None:
        raise RuntimeError("gate bug")


@pytest.mark.asyncio
async def test_unexpected_gate_error_still_propagates(tmp_path: Path) -> None:
    events = EventLog(tmp_path / "events.jsonl")
    agent = RecoveryAgent(
        llm=FakeLLM(),
        tools=ExplodingGateTools(unhealthy=True, execution_ok=True),  # type: ignore[arg-type]
        verifier=FakeVerifier(healthy=True),  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=_primary_jev(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="gate bug"):
        await agent.run_iteration()

    assert agent.policy == {}
    assert not [r for r in _steps(events) if r["step"] in {"security.gate", "attempt.result"}]


def test_gate_rejection_policy_key_matches_registered_tool_name(settings) -> None:  # type: ignore[no-untyped-def]
    spec = DynamicToolSpec(
        name="  Restore latest backup ",
        rationale="r",
        operation="request_backup_restore",
        params={"backup_id": "latest"},
    )
    executor = ToolExecutor(settings)

    assert base_tool_name(spec) == executor.register_temporary_dynamic_tool("inc-1", spec)
    assert base_tool_name(spec.model_copy(update={"name": "  "})) == "dynamic_tool"


@pytest.mark.asyncio
async def test_dynamic_tool_executed_payload_nests_action_result(tmp_path: Path) -> None:
    tools = FakeTools(unhealthy=True, execution_ok=True, monitor_health_sequence=[False, True])
    events = EventLog(tmp_path / "events.jsonl")
    agent = RecoveryAgent(
        llm=FakeLLM(),
        tools=tools,  # type: ignore[arg-type]
        verifier=FakeVerifier(healthy=True),  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=_primary_jev(),  # type: ignore[arg-type]
    )

    await agent.run_iteration()

    executed = [
        item.payload for item in events.events if item.event_type == "dynamic_tool.executed"
    ]
    assert executed == [
        {
            "incident_id": executed[0]["incident_id"],
            "tool_name": "restore",
            "operation": "request_backup_restore",
            "ok": True,
            "error": None,
            "result": {"ok": True, "tool": "restore", "error": None},
        }
    ]


@pytest.mark.asyncio
async def test_failed_verification_emits_failure_persist_event(tmp_path: Path) -> None:
    tools = FakeTools(unhealthy=True, execution_ok=True)
    events = EventLog(tmp_path / "events.jsonl")
    agent = RecoveryAgent(
        llm=FakeLLM(),
        tools=tools,  # type: ignore[arg-type]
        verifier=FakeVerifier(healthy=False),  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=_primary_jev(),  # type: ignore[arg-type]
    )

    await agent.run_iteration()

    persist = [row for row in _steps(events) if str(row.get("step")).startswith("persist.")]
    assert [(r["step"], r["status"], r["action_key"], r["reward"]) for r in persist] == [
        ("persist.policy_store", "failure", "restore", -1.0)
    ]
    assert agent.policy["restore"] == {"attempts": 1.0, "reward_total": -1.0}
    names = [str(row.get("step")) for row in _steps(events)]
    assert names.index("persist.policy_store") < names.index("attempt.result")


@pytest.mark.asyncio
async def test_unusable_decision_emits_failure_persist_event(tmp_path: Path) -> None:
    events = EventLog(tmp_path / "events.jsonl")
    agent = RecoveryAgent(
        llm=FakeLLM(),
        tools=FakeTools(unhealthy=True, execution_ok=True),  # type: ignore[arg-type]
        verifier=FakeVerifier(healthy=True),  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=FakeJev(JevDecision(usable=False, reason="timeout")),  # type: ignore[arg-type]
    )

    await agent.run_iteration()

    persist = [row for row in _steps(events) if str(row.get("step")).startswith("persist.")]
    assert [(r["step"], r["status"], r["action_key"]) for r in persist] == [
        ("persist.policy_store", "failure", "decision_unusable")
    ]


@pytest.mark.asyncio
async def test_jev_request_error_emits_no_persist_event(tmp_path: Path) -> None:
    events = EventLog(tmp_path / "events.jsonl")
    agent = RecoveryAgent(
        llm=FakeLLM(),
        tools=FakeTools(unhealthy=True, execution_ok=True),  # type: ignore[arg-type]
        verifier=FakeVerifier(healthy=True),  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=FakeJev(
            JevDecision(usable=False, reason="jev_request_error:TypeSafeAPITimeoutError")
        ),  # type: ignore[arg-type]
    )

    await agent.run_iteration()

    assert not [row for row in _steps(events) if str(row.get("step")).startswith("persist.")]


@pytest.mark.asyncio
async def test_retry_closes_incident_without_learning_when_target_already_healthy(
    tmp_path: Path,
) -> None:
    # Attempt 1 fails verification; the target is repaired outside MI before the retry.
    tools = FakeTools(unhealthy=True, execution_ok=True, monitor_health_sequence=[False, True])
    events = EventLog(tmp_path / "events.jsonl")
    agent = RecoveryAgent(
        llm=FakeLLM(),
        tools=tools,  # type: ignore[arg-type]
        verifier=FakeVerifier(healthy=False),  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=_primary_jev(),  # type: ignore[arg-type]
    )

    await agent.run_iteration()
    policy_after_failure = agent.policy
    await agent.run_iteration()

    assert tools.executions == 1  # no second action
    assert tools.persisted == []
    assert agent.policy == policy_after_failure  # no reward for the external repair
    assert not (tmp_path / "skills.md").exists()
    snapshot = agent.snapshot
    assert snapshot.incident is None
    assert snapshot.workflow_state.value == "recovered"
    closed = [item.payload for item in events.events if item.event_type == "incident.closed"]
    assert [row.get("resolution") for row in closed] == ["healthy_before_retry"]
    assert not [item for item in events.events if item.event_type == "incident.recovered"]
    last_step = _steps(events)[-1]
    assert (last_step["step"], last_step["status"], last_step["attempt"]) == (
        "monitor.healthy_before_retry",
        "healthy",
        1,
    )

    # The next healthy check settles into the steady state.
    await agent.run_iteration()
    assert agent.snapshot.workflow_state.value == "healthy"


@pytest.mark.asyncio
async def test_retry_still_runs_when_target_remains_unhealthy(tmp_path: Path) -> None:
    tools = FakeTools(unhealthy=True, execution_ok=True)
    events = EventLog(tmp_path / "events.jsonl")
    agent = RecoveryAgent(
        llm=FakeLLM(),
        tools=tools,  # type: ignore[arg-type]
        verifier=FakeVerifier(healthy=False),  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=_primary_jev(),  # type: ignore[arg-type]
    )

    await agent.run_iteration()
    await agent.run_iteration()

    assert tools.executions == 2
    retry = [row for row in _steps(events) if row["attempt"] == 2]
    assert [row["step"] for row in retry[:2]] == ["context.reused", "class_memory.lookup"]
    monitors = [item for item in events.events if item.event_type == "monitor.health"]
    assert len(monitors) == 2  # detection + pre-retry check
