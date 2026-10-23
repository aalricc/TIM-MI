"""Runtime scenarios for harness tests.

Each scenario drives the real RecoveryAgent with scripted collaborators and returns the
runtime's on-disk outputs (events.jsonl, class_store.json, skills.md), so harness
contracts, mappings, sensors and KPIs are checked against what the runtime emits.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.mappings.runtime import load_event_log
from mi_agent.agent.engine import RecoveryAgent
from mi_agent.class_memory import ClassMemory, ClassMemoryConfig
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

DIAGNOSTICS: dict[str, Any] = {
    "health_deep": {"ok": False, "error": "schema_invalid", "status_code": 500},
    "db_status": {"ok": False, "error": "schema_invalid", "widgets_table_present": False},
    "log_tail": ['{"level": "error", "message": "health deep failed"}'],
}
DIAGNOSIS = Diagnosis(
    failure_type="schema_drift_missing_required_table",
    hypotheses=["widgets table dropped"],
    evidence=["widgets_table_present=false"],
)
PRIMARY = DynamicToolSpec(
    name="recreate_widgets",
    rationale="restore schema",
    operation="run_sql",
    params={"db_path": "data/app.db", "sql": "CREATE TABLE IF NOT EXISTS widgets(id INTEGER);"},
)
ALTERNATIVES = [
    DynamicToolSpec(
        name="restore_backup",
        rationale="reset db",
        operation="request_backup_restore",
        params={"backup_id": "latest"},
    ),
    # Not on the security-gate allowlist: LLM candidates carry free-text operations.
    DynamicToolSpec(
        name="restart_container",
        rationale="bounce",
        operation="restart_container",
        params={"container": "target-app"},
    ),
]
FAST_PATH_SPEC = DynamicToolSpec(
    name="class_memory_recreate",
    rationale="learned",
    operation="run_sql",
    params={"db_path": "data/app.db", "sql": "CREATE TABLE IF NOT EXISTS widgets(id INT);"},
)

JevStep = Callable[[dict[str, Any], list[dict[str, Any]]], JevDecision]


GATE_ALLOWLIST = {"request_backup_restore", "restart_service", "run_sql", "replace_file"}


def select_first(state: dict[str, Any], candidates: list[dict[str, Any]]) -> JevDecision:
    """Select the first candidate the real security gate would accept."""
    del state
    chosen = next(row for row in candidates if row["operation"] in GATE_ALLOWLIST)
    return JevDecision(
        usable=True,
        reason="usable",
        selected_candidate_name=str(chosen["name"]),
        selected_candidate_operation=str(chosen["operation"]),
        usable_probability=0.9,
        risk_score=0.2,
        model_version="jev-test",
    )


def unusable(reason: str) -> JevStep:
    def step(state: dict[str, Any], candidates: list[dict[str, Any]]) -> JevDecision:
        del state, candidates
        return JevDecision(usable=False, reason=reason, usable_probability=0.1)

    return step


def select_unknown(state: dict[str, Any], candidates: list[dict[str, Any]]) -> JevDecision:
    del state, candidates
    return JevDecision(
        usable=True,
        reason="usable",
        selected_candidate_name="not_a_candidate",
        selected_candidate_operation="not_an_operation",
        usable_probability=0.8,
        risk_score=0.3,
    )


def reject_source(source: str) -> JevStep:
    def step(state: dict[str, Any], candidates: list[dict[str, Any]]) -> JevDecision:
        if state.get("source") == source:
            return JevDecision(usable=False, reason=f"reject:{source}")
        return select_first(state, candidates)

    return step


class ScriptedLLM:
    async def diagnose_why(self, *, evidence: dict[str, Any], rag_chunks: list[Any]) -> Diagnosis:
        del evidence, rag_chunks
        return DIAGNOSIS

    async def propose_how(self, **kwargs: Any) -> HowProposal:
        del kwargs
        return HowProposal(primary=PRIMARY, alternatives=list(ALTERNATIVES))


class ScriptedTools:
    def __init__(
        self,
        *,
        monitor: list[bool],
        execute_ok: list[bool],
        rag_meta: list[dict[str, Any]] | None = None,
        gate_rejects: set[str] | None = None,
    ) -> None:
        self._gate_rejects = set(gate_rejects or ())
        self._monitor = list(monitor)
        self._execute_ok = list(execute_ok)
        self._rag_meta = list(rag_meta or [])
        self._pending_meta: dict[str, Any] = {}
        self.executions = 0

    async def monitor_health(self) -> MonitorStatus:
        healthy = self._monitor.pop(0) if len(self._monitor) > 1 else self._monitor[0]
        return MonitorStatus(
            healthy=healthy,
            status_code=200 if healthy else 500,
            detail="healthy" if healthy else "health_endpoint_unhealthy",
        )

    async def collect_diagnostics(self) -> dict[str, Any]:
        return json.loads(json.dumps(DIAGNOSTICS))

    async def retrieve_rag_chunks(self, query: str, *, k: int) -> list[Any]:
        del k
        meta = self._rag_meta.pop(0) if self._rag_meta else {"status": "empty", "count": 0}
        self._pending_meta = {"query_used": query, **meta}
        return []

    def consume_last_rag_meta(self) -> dict[str, Any]:
        meta, self._pending_meta = self._pending_meta, {}
        return meta

    def security_gate(self, spec: DynamicToolSpec) -> None:
        if spec.name in self._gate_rejects:
            raise SecurityGateError(f"path escapes app root: {spec.name}")

    def register_temporary_dynamic_tool(self, incident_id: str, spec: DynamicToolSpec) -> str:
        del incident_id
        return spec.name

    async def execute_dynamic_tool(self, incident_id: str, tool_name: str) -> dict[str, Any]:
        del incident_id
        self.executions += 1
        ok = self._execute_ok.pop(0) if self._execute_ok else True
        return {"ok": ok, "action": tool_name, "error": None if ok else "sql_failed"}

    def persist_dynamic_tool(self, incident_id: str, tool_name: str) -> None:
        del incident_id, tool_name

    def discard_incident_tools(self, incident_id: str, *, keep: str | None = None) -> None:
        del incident_id, keep

    def state_fingerprint(self) -> str:
        return "fp"


class ScriptedVerifier:
    def __init__(self, healthy: list[bool]) -> None:
        self._healthy = list(healthy)

    async def verify(self) -> VerificationResult:
        healthy = self._healthy.pop(0) if self._healthy else True
        return VerificationResult(
            healthy=healthy,
            failure_type=None if healthy else "schema_invalid",
            details={"ok": healthy},
        )


class ScriptedJev:
    def __init__(self, steps: list[JevStep]) -> None:
        self._steps = list(steps)

    async def choose_candidate(
        self, *, state: dict[str, Any], candidates: list[dict[str, Any]]
    ) -> JevDecision:
        step = self._steps.pop(0) if self._steps else select_first
        return step(state, candidates)


@dataclass(slots=True)
class ScenarioRun:
    name: str
    state_dir: Path
    executions: int

    @property
    def events(self) -> list[dict[str, Any]]:
        return load_event_log(self.state_dir / "events.jsonl")

    @property
    def class_store(self) -> dict[str, Any]:
        path = self.state_dir / "class_store.json"
        if not path.exists():
            return {"classes": {}}
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return payload

    @property
    def skills_text(self) -> str:
        path = self.state_dir / "skills.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""


def _class_memory(state_dir: Path, *, seed_successes: int) -> ClassMemory:
    memory = ClassMemory(
        ClassMemoryConfig(
            store_path=state_dir / "class_store.json",
            join_threshold=0.82,
            margin_delta=0.08,
            tau_conf=0.80,
            n_min=3,
            cooldown_seconds=30.0,
            embedding_dim=64,
            min_similarity_for_id=0.2,
        )
    )
    if seed_successes:
        signature = memory.build_signature(diagnostics=DIAGNOSTICS, diagnosis=DIAGNOSIS, attempts=0)
        match = memory.assign_class(signature)
        for _ in range(seed_successes):
            memory.record_outcome(
                match=match, spec=FAST_PATH_SPEC, latency_ms=3.0, success=True, source="full_loop"
            )
    return memory


async def run_scenario(
    state_dir: Path,
    *,
    name: str,
    iterations: int,
    monitor: list[bool],
    execute_ok: list[bool],
    verify: list[bool],
    jev: list[JevStep],
    class_memory: bool = False,
    seed_successes: int = 0,
    rag_meta: list[dict[str, Any]] | None = None,
    gate_rejects: set[str] | None = None,
) -> ScenarioRun:
    state_dir.mkdir(parents=True, exist_ok=True)
    tools = ScriptedTools(
        monitor=monitor, execute_ok=execute_ok, rag_meta=rag_meta, gate_rejects=gate_rejects
    )
    memory = _class_memory(state_dir, seed_successes=seed_successes) if class_memory else None
    agent = RecoveryAgent(
        llm=ScriptedLLM(),  # type: ignore[arg-type]
        tools=tools,  # type: ignore[arg-type]
        verifier=ScriptedVerifier(verify),  # type: ignore[arg-type]
        events=EventLog(state_dir / "events.jsonl"),
        policy_store_path=(state_dir / "policy_store.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=ScriptedJev(jev),  # type: ignore[arg-type]
        class_memory=memory,
        classmem_enabled=class_memory,
        classmem_dedupe_enabled=True,
        classmem_fastpath_enabled=True,
    )
    for _ in range(iterations):
        await agent.run_iteration()
    return ScenarioRun(name=name, state_dir=state_dir, executions=tools.executions)


ScenarioFactory = Callable[[Path], Awaitable[ScenarioRun]]


async def full_loop_success(root: Path) -> ScenarioRun:
    return await run_scenario(
        root / "full_loop_success",
        name="full_loop_success",
        iterations=1,
        monitor=[False, True],
        execute_ok=[True],
        verify=[True],
        jev=[select_first],
    )


async def unusable_decisions_then_success(root: Path) -> ScenarioRun:
    return await run_scenario(
        root / "unusable_decisions_then_success",
        name="unusable_decisions_then_success",
        iterations=4,
        monitor=[False, True],
        execute_ok=[True],
        verify=[True],
        jev=[
            unusable("low_confidence"),
            unusable("jev_request_error:TypeSafeAPITimeoutError"),
            select_unknown,
            select_first,
        ],
    )


async def failure_then_unhealthy_ping_then_success(root: Path) -> ScenarioRun:
    return await run_scenario(
        root / "failure_then_unhealthy_ping_then_success",
        name="failure_then_unhealthy_ping_then_success",
        iterations=3,
        monitor=[False, False, True],
        execute_ok=[False, True, True],
        verify=[False, True, True],
        jev=[select_first, select_first, select_first],
        rag_meta=[
            {"status": "skipped", "reason": "rag_disabled", "count": 0},
            {
                "status": "error",
                "reason": "search_failed",
                "count": 0,
                "error_type": "TimeoutError",
                "error_message": "pinecone timeout",
            },
            {"status": "ok", "reason": "search_ok", "count": 0},
        ],
    )


async def fast_path_failure_then_full_loop(root: Path) -> ScenarioRun:
    return await run_scenario(
        root / "fast_path_failure_then_full_loop",
        name="fast_path_failure_then_full_loop",
        iterations=2,
        monitor=[False, True],
        execute_ok=[False, True],
        verify=[False, True],
        jev=[select_first, select_first],
        class_memory=True,
        seed_successes=8,
    )


async def fast_path_rejected_falls_back(root: Path) -> ScenarioRun:
    return await run_scenario(
        root / "fast_path_rejected_falls_back",
        name="fast_path_rejected_falls_back",
        iterations=1,
        monitor=[False, True],
        execute_ok=[True],
        verify=[True],
        jev=[reject_source("class_memory"), select_first],
        class_memory=True,
        seed_successes=8,
    )


async def class_memory_cold_start(root: Path) -> ScenarioRun:
    return await run_scenario(
        root / "class_memory_cold_start",
        name="class_memory_cold_start",
        iterations=1,
        monitor=[False, True],
        execute_ok=[True],
        verify=[True],
        jev=[select_first],
        class_memory=True,
    )


async def gate_rejected_then_success(root: Path) -> ScenarioRun:
    # The learned fast-path template fails the security gate; the retry excludes it and
    # the full loop recovers.
    return await run_scenario(
        root / "gate_rejected_then_success",
        name="gate_rejected_then_success",
        iterations=2,
        monitor=[False, True],
        execute_ok=[True],
        verify=[True],
        jev=[select_first, select_first],
        class_memory=True,
        seed_successes=8,
        gate_rejects={FAST_PATH_SPEC.name},
    )


SCENARIOS: dict[str, ScenarioFactory] = {
    "full_loop_success": full_loop_success,
    "unusable_decisions_then_success": unusable_decisions_then_success,
    "failure_then_unhealthy_ping_then_success": failure_then_unhealthy_ping_then_success,
    "fast_path_failure_then_full_loop": fast_path_failure_then_full_loop,
    "fast_path_rejected_falls_back": fast_path_rejected_falls_back,
    "class_memory_cold_start": class_memory_cold_start,
    "gate_rejected_then_success": gate_rejected_then_success,
}
