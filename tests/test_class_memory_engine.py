from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mi_agent.agent.engine import RecoveryAgent
from mi_agent.class_memory import ClassMatch, ClassMemory, ClassMemoryConfig, canonical_action_key
from mi_agent.decision.client import JevDecision
from mi_agent.errors import SecurityGateError
from mi_agent.events import EventLog
from mi_agent.workflow.models import (
    Diagnosis,
    DynamicToolSpec,
    HowProposal,
    MonitorStatus,
    VerificationResult,
)


class EngineLLM:
    def __init__(self) -> None:
        self.diagnosis = Diagnosis(
            failure_type="generic_failure",
            hypotheses=["h"],
            evidence=["e"],
        )

    async def diagnose_why(self, *, evidence: dict[str, Any], rag_chunks: list[Any]) -> Diagnosis:
        del evidence, rag_chunks
        return self.diagnosis

    async def propose_how(
        self,
        *,
        diagnosis: Diagnosis,
        rag_chunks: list[Any],
        prior_attempts: list[Any],
        latest_tool_error: str | None,
        state_fingerprint: str | None,
        backup_catalog: list[Any],
    ) -> HowProposal:
        del backup_catalog
        del diagnosis, rag_chunks, prior_attempts, latest_tool_error, state_fingerprint
        return HowProposal(
            primary=DynamicToolSpec(
                name="full-loop-primary",
                rationale="r",
                operation="run_sql",
                params={
                    "db_path": "data/app.db",
                    "sql": "CREATE TABLE IF NOT EXISTS t(id INTEGER PRIMARY KEY);",
                },
            ),
            alternatives=[
                DynamicToolSpec(
                    name="full-loop-fallback",
                    rationale="r",
                    operation="request_backup_restore",
                    params={"backup_id": "app-1792854880"},
                ),
                DynamicToolSpec(
                    name="full-loop-other",
                    rationale="r",
                    operation="restart_service",
                    params={"service_name": "svc"},
                ),
            ],
        )

    def consume_trace(self) -> dict[str, Any]:
        return {"request": {}, "response": {}}


class EngineTools:
    def __init__(self, *, execute_ok_sequence: list[bool]) -> None:
        self._execute_ok_sequence = list(execute_ok_sequence)
        self.executed_specs: list[DynamicToolSpec] = []
        self.catalog: dict[str, dict[str, Any]] = {}
        self.security_gate_runs = 0

    async def monitor_health(self) -> MonitorStatus:
        return MonitorStatus(healthy=False, status_code=500, detail="down")

    async def collect_diagnostics(self) -> dict[str, Any]:
        return {
            "health_deep": {"ok": False, "error": "generic_failure"},
            "db_status": {"ok": False, "error": "generic_failure"},
            "log_tail": ["token 111 path /tmp/a"],
        }

    async def retrieve_rag_chunks(self, query: str, *, k: int) -> list[Any]:
        del query, k
        return []

    def security_gate(self, spec: DynamicToolSpec) -> None:
        del spec

    def register_temporary_dynamic_tool(self, incident_id: str, spec: DynamicToolSpec) -> str:
        del incident_id
        self.catalog[spec.name] = spec.model_dump()
        self.executed_specs.append(spec)
        return spec.name

    async def execute_dynamic_tool(self, incident_id: str, tool_name: str) -> dict[str, Any]:
        del incident_id, tool_name
        self.security_gate_runs += 1
        ok = self._execute_ok_sequence.pop(0) if self._execute_ok_sequence else True
        return {"ok": ok, "error": None if ok else "failed"}

    def discard_incident_tools(self, incident_id: str, *, keep: str | None = None) -> None:
        del incident_id, keep

    def persist_dynamic_tool(self, incident_id: str, tool_name: str) -> None:
        del incident_id, tool_name

    def state_fingerprint(self) -> str:
        return "fp"


class EngineVerifier:
    def __init__(self, *, healthy_sequence: list[bool]) -> None:
        self._healthy_sequence = list(healthy_sequence)

    async def verify(self) -> VerificationResult:
        healthy = self._healthy_sequence.pop(0) if self._healthy_sequence else True
        return VerificationResult(
            healthy=healthy,
            failure_type=None if healthy else "still_bad",
            details={"ok": healthy},
        )


class EngineJev:
    def __init__(self, *, reject_sources: set[str] | None = None) -> None:
        self.reject_sources = reject_sources or set()
        self.states: list[dict[str, Any]] = []

    async def choose_candidate(
        self,
        *,
        state: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> JevDecision:
        self.states.append(dict(state))
        source = str(state.get("source") or "full_loop")
        if source in self.reject_sources:
            return JevDecision(usable=False, reason=f"reject:{source}")
        selected = candidates[0] if candidates else {}
        return JevDecision(
            usable=True,
            selected_candidate_name=str(selected.get("name") or ""),
            selected_candidate_operation=str(selected.get("operation") or ""),
            usable_probability=0.95,
            risk_score=0.1,
        )


class EngineJevRejectClassMemoryContext:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def choose_candidate(
        self,
        *,
        state: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> JevDecision:
        self.calls.append(dict(state))
        if "class_memory_candidate" in state:
            return JevDecision(
                usable=False,
                reason="jev_request_error:TypeSafeUnprocessableEntityError",
            )
        selected = candidates[0] if candidates else {}
        return JevDecision(
            usable=True,
            selected_candidate_name=str(selected.get("name") or ""),
            selected_candidate_operation=str(selected.get("operation") or ""),
            usable_probability=0.95,
            risk_score=0.1,
        )


class EngineJevRejectClassMemorySource:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def choose_candidate(
        self,
        *,
        state: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> JevDecision:
        self.calls.append(dict(state))
        if state.get("source") == "class_memory":
            return JevDecision(
                usable=False,
                reason="jev_request_error:TypeSafeUnprocessableEntityError",
            )
        selected = candidates[0] if candidates else {}
        return JevDecision(
            usable=True,
            selected_candidate_name=str(selected.get("name") or ""),
            selected_candidate_operation=str(selected.get("operation") or ""),
            usable_probability=0.95,
            risk_score=0.1,
        )


def _empty_class_memory(tmp_path: Path) -> ClassMemory:
    return ClassMemory(
        ClassMemoryConfig(
            store_path=tmp_path / "class_store.json",
            join_threshold=0.82,
            margin_delta=0.08,
            tau_conf=0.80,
            n_min=3,
            cooldown_seconds=2.0,
            embedding_dim=64,
            min_similarity_for_id=0.2,
        )
    )


def _class_memory(
    tmp_path: Path,
    llm: EngineLLM,
    class_spec: DynamicToolSpec | None = None,
) -> tuple[ClassMemory, ClassMatch, DynamicToolSpec]:
    memory = ClassMemory(
        ClassMemoryConfig(
            store_path=tmp_path / "class_store.json",
            join_threshold=0.82,
            margin_delta=0.08,
            tau_conf=0.80,
            n_min=3,
            cooldown_seconds=2.0,
            embedding_dim=64,
            min_similarity_for_id=0.2,
        )
    )
    diagnostics = {
        "health_deep": {"ok": False, "error": "generic_failure"},
        "db_status": {"ok": False, "error": "generic_failure"},
        "log_tail": ["token 111 path /tmp/a"],
    }
    signature = memory.build_signature(
        diagnostics=diagnostics,
        diagnosis=llm.diagnosis,
        attempts=0,
    )
    match = memory.assign_class(signature)
    class_spec = class_spec or DynamicToolSpec(
        name="class-memory-fast",
        rationale="r",
        operation="run_sql",
        params={
            "db_path": "data/app.db",
            "sql": "CREATE TABLE IF NOT EXISTS t(id INTEGER PRIMARY KEY);",
        },
    )
    for _ in range(8):
        memory.record_outcome(
            match=match,
            spec=class_spec,
            latency_ms=2.0,
            success=True,
            source="full_loop",
        )
    return memory, match, class_spec


@pytest.mark.asyncio
async def test_fast_path_failure_falls_back_and_excludes_failed_action(tmp_path: Path) -> None:
    llm = EngineLLM()
    memory, _, class_spec = _class_memory(tmp_path, llm)

    tools = EngineTools(execute_ok_sequence=[False, True])
    verifier = EngineVerifier(healthy_sequence=[False, True])
    events = EventLog(tmp_path / "events.jsonl")
    agent = RecoveryAgent(
        llm=llm,
        tools=tools,  # type: ignore[arg-type]
        verifier=verifier,  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=EngineJev(),  # type: ignore[arg-type]
        class_memory=memory,
        classmem_enabled=True,
        classmem_dedupe_enabled=True,
        classmem_fastpath_enabled=True,
    )

    await agent.run_iteration()
    await agent.run_iteration()

    assert tools.security_gate_runs == 2
    assert len(tools.executed_specs) >= 2
    first_key = canonical_action_key(tools.executed_specs[0])
    assert first_key == canonical_action_key(class_spec)
    second_key = canonical_action_key(tools.executed_specs[1])
    assert second_key != first_key

    planning = [item for item in events.events if item.event_type == "planning.how"]
    assert len(planning) >= 2
    assert planning[0].payload.get("source") == "class_memory"
    assert planning[1].payload.get("source") == "full_loop"

    workflow_steps = [item.payload for item in events.events if item.event_type == "workflow.step"]
    fast_path_selected = [
        row
        for row in workflow_steps
        if row.get("step") == "path.selected" and row.get("path_mode") == "fast_path"
    ]
    assert fast_path_selected

    full_loop_selected = [
        row
        for row in workflow_steps
        if row.get("step") == "path.selected" and row.get("path_mode") == "full_loop"
    ]
    assert full_loop_selected

    assert any(
        row.get("step") == "jev.requested" and row.get("path_mode") == "fast_path"
        for row in workflow_steps
    )
    assert any(
        row.get("step") == "jev.requested" and row.get("path_mode") == "full_loop"
        for row in workflow_steps
    )


@pytest.mark.asyncio
async def test_gate_rejected_candidate_is_never_executed(tmp_path: Path) -> None:
    llm = EngineLLM()
    memory, _, _ = _class_memory(tmp_path, llm)

    tools = EngineTools(execute_ok_sequence=[True, True])
    verifier = EngineVerifier(healthy_sequence=[True, True])
    events = EventLog(tmp_path / "events.jsonl")
    agent = RecoveryAgent(
        llm=llm,
        tools=tools,  # type: ignore[arg-type]
        verifier=verifier,  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=EngineJev(reject_sources={"class_memory", "full_loop"}),  # type: ignore[arg-type]
        class_memory=memory,
        classmem_enabled=True,
        classmem_dedupe_enabled=True,
        classmem_fastpath_enabled=True,
    )

    await agent.run_iteration()

    assert tools.security_gate_runs == 0
    skip_events = [
        item
        for item in events.events
        if item.event_type == "class_memory.fast_path_skipped"
    ]
    assert len(skip_events) == 1
    assert skip_events[0].payload.get("reason") == "reject:class_memory"


@pytest.mark.asyncio
async def test_fast_path_candidate_context_passed_to_jev(tmp_path: Path) -> None:
    llm = EngineLLM()
    memory, _, class_spec = _class_memory(tmp_path, llm)

    tools = EngineTools(execute_ok_sequence=[True])
    verifier = EngineVerifier(healthy_sequence=[True])
    jev = EngineJev()
    agent = RecoveryAgent(
        llm=llm,
        tools=tools,  # type: ignore[arg-type]
        verifier=verifier,  # type: ignore[arg-type]
        events=EventLog(tmp_path / "events.jsonl"),
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=jev,  # type: ignore[arg-type]
        class_memory=memory,
        classmem_enabled=True,
        classmem_dedupe_enabled=True,
        classmem_fastpath_enabled=True,
    )

    await agent.run_iteration()

    classmem_states = [state for state in jev.states if state.get("source") == "class_memory"]
    assert classmem_states
    context = classmem_states[0].get("class_memory_candidate")
    assert isinstance(context, dict)
    assert context.get("action_key") == canonical_action_key(class_spec)
    champion_stats = context.get("champion_stats")
    assert isinstance(champion_stats, dict)
    assert int(champion_stats.get("attempts", 0)) >= 8


@pytest.mark.asyncio
async def test_fast_path_retries_without_context_after_jev_request_error(tmp_path: Path) -> None:
    llm = EngineLLM()
    memory, _, _ = _class_memory(tmp_path, llm)

    tools = EngineTools(execute_ok_sequence=[True])
    verifier = EngineVerifier(healthy_sequence=[True])
    jev = EngineJevRejectClassMemoryContext()
    events = EventLog(tmp_path / "events.jsonl")
    agent = RecoveryAgent(
        llm=llm,
        tools=tools,  # type: ignore[arg-type]
        verifier=verifier,  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=jev,  # type: ignore[arg-type]
        class_memory=memory,
        classmem_enabled=True,
        classmem_dedupe_enabled=True,
        classmem_fastpath_enabled=True,
    )

    await agent.run_iteration()

    assert len(jev.calls) >= 2
    assert "class_memory_candidate" in jev.calls[0]
    assert "class_memory_candidate" not in jev.calls[1]
    assert tools.security_gate_runs == 1

    skip_events = [
        item
        for item in events.events
        if item.event_type == "class_memory.fast_path_skipped"
    ]
    assert not skip_events


@pytest.mark.asyncio
async def test_fast_path_retries_without_source_after_jev_request_error(tmp_path: Path) -> None:
    llm = EngineLLM()
    memory, _, _ = _class_memory(tmp_path, llm)

    tools = EngineTools(execute_ok_sequence=[True])
    verifier = EngineVerifier(healthy_sequence=[True])
    jev = EngineJevRejectClassMemorySource()
    events = EventLog(tmp_path / "events.jsonl")
    agent = RecoveryAgent(
        llm=llm,
        tools=tools,  # type: ignore[arg-type]
        verifier=verifier,  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=jev,  # type: ignore[arg-type]
        class_memory=memory,
        classmem_enabled=True,
        classmem_dedupe_enabled=True,
        classmem_fastpath_enabled=True,
    )

    await agent.run_iteration()

    assert len(jev.calls) >= 3
    assert jev.calls[0].get("source") == "class_memory"
    assert jev.calls[1].get("source") == "class_memory"
    assert "source" not in jev.calls[2]
    assert tools.security_gate_runs == 1

    skip_events = [
        item
        for item in events.events
        if item.event_type == "class_memory.fast_path_skipped"
    ]
    assert not skip_events


@pytest.mark.asyncio
async def test_flag_off_keeps_baseline_event_shape(tmp_path: Path) -> None:
    llm = EngineLLM()
    tools = EngineTools(execute_ok_sequence=[True])
    verifier = EngineVerifier(healthy_sequence=[True])
    events = EventLog(tmp_path / "events.jsonl")
    agent = RecoveryAgent(
        llm=llm,
        tools=tools,  # type: ignore[arg-type]
        verifier=verifier,  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=EngineJev(),  # type: ignore[arg-type]
        class_memory=None,
        classmem_enabled=False,
    )

    await agent.run_iteration()

    planning = [item for item in events.events if item.event_type == "planning.how"]
    assert len(planning) == 1
    payload_keys = set(planning[0].payload.keys())
    assert payload_keys == {
        "incident_id",
        "executed_tool_name",
        "primary",
        "alternatives",
        "executed_candidate",
        "jev_decision",
    }


@pytest.mark.asyncio
async def test_failed_fast_path_emits_class_memory_failure_persist(tmp_path: Path) -> None:
    llm = EngineLLM()
    memory, match, _ = _class_memory(tmp_path, llm)
    events = EventLog(tmp_path / "events.jsonl")
    agent = RecoveryAgent(
        llm=llm,
        tools=EngineTools(execute_ok_sequence=[False]),  # type: ignore[arg-type]
        verifier=EngineVerifier(healthy_sequence=[False]),  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=EngineJev(),  # type: ignore[arg-type]
        class_memory=memory,
        classmem_enabled=True,
        classmem_dedupe_enabled=True,
        classmem_fastpath_enabled=True,
    )

    await agent.run_iteration()

    persist = [
        item.payload
        for item in events.events
        if item.event_type == "workflow.step"
        and str(item.payload.get("step")).startswith("persist.")
    ]
    assert [(row["step"], row["status"], row["path_mode"]) for row in persist] == [
        ("persist.policy_store", "failure", "fast_path"),
        ("persist.class_memory", "failure", "fast_path"),
    ]
    assert persist[1]["class_id"] == match.class_id


@pytest.mark.asyncio
async def test_first_success_for_new_class_writes_skill_section(tmp_path: Path) -> None:
    llm = EngineLLM()
    memory = ClassMemory(
        ClassMemoryConfig(
            store_path=tmp_path / "class_store.json",
            join_threshold=0.82,
            margin_delta=0.08,
            tau_conf=0.80,
            n_min=3,
            cooldown_seconds=2.0,
            embedding_dim=64,
            min_similarity_for_id=0.2,
        )
    )
    events = EventLog(tmp_path / "events.jsonl")
    agent = RecoveryAgent(
        llm=llm,
        tools=EngineTools(execute_ok_sequence=[True]),  # type: ignore[arg-type]
        verifier=EngineVerifier(healthy_sequence=[True]),  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=EngineJev(),  # type: ignore[arg-type]
        class_memory=memory,
        classmem_enabled=True,
        classmem_dedupe_enabled=True,
        classmem_fastpath_enabled=True,
    )

    await agent.run_iteration()

    skills_rows = [
        item.payload
        for item in events.events
        if item.event_type == "workflow.step" and item.payload.get("step") == "persist.skills"
    ]
    assert [row["status"] for row in skills_rows] == ["ok"]
    class_id = skills_rows[0]["class_id"]
    skills_text = (tmp_path / "skills.md").read_text(encoding="utf-8")
    assert f"<!-- class:{class_id} begin -->" in skills_text
    assert memory.render_skills_section(class_id=class_id) in skills_text


class GateRejectingEngineTools(EngineTools):
    def __init__(self, *, rejected_names: set[str], execute_ok_sequence: list[bool]) -> None:
        super().__init__(execute_ok_sequence=execute_ok_sequence)
        self.rejected_names = rejected_names

    def security_gate(self, spec: DynamicToolSpec) -> None:
        if spec.name in self.rejected_names:
            raise SecurityGateError(f"path escapes app root: {spec.name}")


@pytest.mark.asyncio
async def test_gate_rejected_fast_path_is_penalised_and_falls_back(tmp_path: Path) -> None:
    llm = EngineLLM()
    memory, match, class_spec = _class_memory(tmp_path, llm)
    key = canonical_action_key(class_spec)
    before = memory.action_stats(class_id=match.class_id, action_key=key)
    assert before is not None
    tools = GateRejectingEngineTools(rejected_names={class_spec.name}, execute_ok_sequence=[True])
    events = EventLog(tmp_path / "events.jsonl")
    agent = RecoveryAgent(
        llm=llm,
        tools=tools,  # type: ignore[arg-type]
        verifier=EngineVerifier(healthy_sequence=[True]),  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=EngineJev(),  # type: ignore[arg-type]
        class_memory=memory,
        classmem_enabled=True,
        classmem_dedupe_enabled=True,
        classmem_fastpath_enabled=True,
    )

    await agent.run_iteration()  # fast path: gate rejects the learned template
    await agent.run_iteration()  # retry: template excluded, full loop recovers

    after = memory.action_stats(class_id=match.class_id, action_key=key)
    assert after is not None
    assert after["beta"] == before["beta"] + 1.0
    assert after["attempts"] == before["attempts"] + 1
    assert len(tools.executed_specs) == 1
    assert tools.executed_specs[0].name != class_spec.name

    steps = [
        (item.payload["attempt"], item.payload["path_mode"], item.payload["step"],
         item.payload["status"])
        for item in events.events
        if item.event_type == "workflow.step"
    ]
    assert (1, "fast_path", "security.gate", "rejected") in steps
    assert (1, "fast_path", "persist.class_memory", "failure") in steps
    assert (1, "fast_path", "attempt.result", "failed") in steps
    # EngineTools.monitor_health is always unhealthy, so the post-recovery ping turns the
    # verified recovery into a retry; verification + success persistence is what matters.
    assert (2, "full_loop", "verification.result", "ok") in steps
    assert (2, "full_loop", "persist.class_memory", "ok") in steps


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


class CatalogTools(EngineTools):
    """EngineTools whose diagnostics carry a target backup catalog."""

    def __init__(self, *, execute_ok_sequence: list[bool], backups: list[dict[str, Any]]) -> None:
        super().__init__(execute_ok_sequence=execute_ok_sequence)
        self.backups = backups

    async def collect_diagnostics(self) -> dict[str, Any]:
        return {**await super().collect_diagnostics(), "backups": self.backups}


class RestoreLLM(EngineLLM):
    """Full loop proposes restores; call N proposes the backups in proposals[N]."""

    def __init__(self, proposals: list[list[str]]) -> None:
        super().__init__()
        self.proposals = proposals
        self.catalogs: list[list[Any]] = []

    async def propose_how(self, **kwargs: Any) -> HowProposal:
        self.catalogs.append(list(kwargs["backup_catalog"]))
        restores = [
            DynamicToolSpec(
                name=f"restore-{backup_id}",
                rationale="r",
                operation="request_backup_restore",
                params={"backup_id": backup_id},
            )
            for backup_id in self.proposals[len(self.catalogs) - 1]
        ]
        return HowProposal(primary=restores[0], alternatives=restores[1:])


def _agent(
    tmp_path: Path,
    *,
    llm: EngineLLM,
    tools: EngineTools,
    verifier: EngineVerifier,
    memory: ClassMemory,
) -> tuple[RecoveryAgent, EventLog]:
    events = EventLog(tmp_path / "events.jsonl")
    agent = RecoveryAgent(
        llm=llm,
        tools=tools,  # type: ignore[arg-type]
        verifier=verifier,  # type: ignore[arg-type]
        events=events,
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=EngineJev(),  # type: ignore[arg-type]
        class_memory=memory,
        classmem_enabled=True,
        classmem_dedupe_enabled=True,
        classmem_fastpath_enabled=True,
    )
    return agent, events


def _learned_restore() -> DynamicToolSpec:
    return DynamicToolSpec(
        name="restore-from-backup",
        rationale="r",
        operation="request_backup_restore",
        params={"backup_id": "app-1700000000"},
    )


@pytest.mark.asyncio
async def test_fast_path_restore_binds_newest_backup_before_detection(tmp_path: Path) -> None:
    llm = EngineLLM()
    memory, _, _ = _class_memory(tmp_path, llm, _learned_restore())
    now = time.time()
    tools = CatalogTools(
        execute_ok_sequence=[True],
        backups=[
            {"backup_id": "app-future", "created_at": _iso(now + 600), "size_bytes": 1},
            {"backup_id": "app-recent", "created_at": _iso(now - 20), "size_bytes": 1},
            {"backup_id": "app-older", "created_at": _iso(now - 100), "size_bytes": 1},
        ],
    )
    agent, events = _agent(
        tmp_path, llm=llm, tools=tools, verifier=EngineVerifier(healthy_sequence=[True]),
        memory=memory,
    )

    await agent.run_iteration()

    assert [spec.params for spec in tools.executed_specs] == [{"backup_id": "app-recent"}]
    planning = [item.payload for item in events.events if item.event_type == "planning.how"]
    assert planning[0].get("source") == "class_memory"


@pytest.mark.asyncio
async def test_fast_path_restore_without_pre_incident_backup_uses_full_loop(
    tmp_path: Path,
) -> None:
    llm = EngineLLM()
    memory, _, _ = _class_memory(tmp_path, llm, _learned_restore())
    tools = CatalogTools(
        execute_ok_sequence=[True],
        backups=[
            {"backup_id": "app-future", "created_at": _iso(time.time() + 600), "size_bytes": 1}
        ],
    )
    agent, events = _agent(
        tmp_path, llm=llm, tools=tools, verifier=EngineVerifier(healthy_sequence=[True]),
        memory=memory,
    )

    await agent.run_iteration()

    steps = [item.payload for item in events.events if item.event_type == "workflow.step"]
    selected = [row for row in steps if row.get("step") == "path.selected"]
    assert [(row["path_mode"], row["reason"]) for row in selected] == [
        ("full_loop", "fast_path_params_unbound")
    ]
    planning = [item.payload for item in events.events if item.event_type == "planning.how"]
    assert [row.get("source") for row in planning] == ["full_loop"]


@pytest.mark.asyncio
async def test_failed_restore_does_not_block_restoring_another_backup(tmp_path: Path) -> None:
    llm = RestoreLLM([["app-wrong"], ["app-wrong", "app-right"]])
    backups = [{"backup_id": "app-right", "created_at": _iso(time.time() - 20), "size_bytes": 1}]
    tools = CatalogTools(execute_ok_sequence=[False, True], backups=backups)
    agent, _ = _agent(
        tmp_path,
        llm=llm,
        tools=tools,
        verifier=EngineVerifier(healthy_sequence=[False, True]),
        memory=_empty_class_memory(tmp_path),
    )

    await agent.run_iteration()
    await agent.run_iteration()

    assert [spec.params for spec in tools.executed_specs] == [
        {"backup_id": "app-wrong"},
        {"backup_id": "app-right"},
    ]
    assert llm.catalogs[0] == backups
    # Both restores are one learned action, whatever backup they named.
    assert canonical_action_key(tools.executed_specs[0]) == canonical_action_key(
        tools.executed_specs[1]
    )
