from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mi_agent.agent.engine import RecoveryAgent
from mi_agent.decision.client import JevDecision
from mi_agent.events import EventLog
from mi_agent.workflow.models import (
    Diagnosis,
    DynamicToolSpec,
    HowProposal,
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
    def __init__(self, *, unhealthy: bool, execution_ok: bool) -> None:
        self.unhealthy = unhealthy
        self.execution_ok = execution_ok
        self.catalog: dict[str, dict[str, Any]] = {}
        self.executions = 0
        self.persisted: list[str] = []
        self.registered_tools: list[str] = []

    async def monitor_health(self) -> MonitorStatus:
        return MonitorStatus(healthy=not self.unhealthy, status_code=500 if self.unhealthy else 200)

    async def collect_diagnostics(self) -> dict[str, Any]:
        return {"health_deep": {"ok": False, "error": "db_corrupt"}, "log_tail": ["err"]}

    async def retrieve_rag_chunks(self, query: str, *, k: int) -> list[Any]:
        del query, k
        return []

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
async def test_failures_continue_without_no_viable_cap(tmp_path: Path) -> None:
    tools = FakeTools(unhealthy=True, execution_ok=False)
    agent = RecoveryAgent(
        llm=FakeLLM(),
        tools=tools,  # type: ignore[arg-type]
        verifier=FakeVerifier(healthy=False),  # type: ignore[arg-type]
        events=EventLog(tmp_path / "events.jsonl"),
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
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
