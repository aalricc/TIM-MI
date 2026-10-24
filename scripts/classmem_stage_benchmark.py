from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from mi_agent.agent.engine import RecoveryAgent
from mi_agent.class_memory import ClassMemory, ClassMemoryConfig, canonical_action_key
from mi_agent.decision.client import JevDecision
from mi_agent.events import EventLog
from mi_agent.workflow.models import (
    Diagnosis,
    DynamicToolSpec,
    HowProposal,
    IncidentContext,
    MonitorStatus,
    VerificationResult,
)


@dataclass(slots=True)
class StageConfig:
    name: str
    classmem_enabled: bool
    dedupe_enabled: bool
    fastpath_enabled: bool


@dataclass(slots=True)
class StageResult:
    stage: str
    incidents: int
    attempts_mean: float
    latency_p50_ms: float
    latency_p95_ms: float
    action_key_entropy_bits: float
    success_rate: float
    blocked_unsafe_actions: int
    fast_path_hit_rate: float
    fast_path_failure_rate: float
    class_count: int
    skills_section_count: int


class BenchLLM:
    def __init__(self) -> None:
        self._incident_index = 0
        self._line = ""
        self._diagnosis = Diagnosis(
            failure_type="generic_failure",
            hypotheses=["h"],
            evidence=["e"],
        )

    def set_incident(self, incident_index: int, line: str) -> None:
        self._incident_index = incident_index
        self._line = line

    async def diagnose_why(
        self,
        *,
        evidence: dict[str, Any],
        rag_chunks: list[Any],
    ) -> Diagnosis:
        del evidence, rag_chunks
        return self._diagnosis

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
        prefix = f"{self._incident_index}"
        candidates = [
            DynamicToolSpec(
                name=f"restart-a-{prefix}",
                rationale="r",
                operation="restart_service",
                params={"service_name": "svc"},
            ),
            DynamicToolSpec(
                name=f"restart-b-{prefix}",
                rationale="r",
                operation="restart_service",
                params={"service_name": "svc"},
            ),
            DynamicToolSpec(
                name=f"replace-{prefix}",
                rationale="r",
                operation="replace_file",
                params={
                    "source_path": "tmp/fix.sql",
                    "destination_path": "app/schema.sql",
                },
            ),
            DynamicToolSpec(
                name=f"sql-fix-{prefix}",
                rationale="r",
                operation="run_sql",
                params={
                    "db_path": "data/app.db",
                    "sql": "CREATE TABLE IF NOT EXISTS t(id INTEGER PRIMARY KEY);",
                },
            ),
        ]
        return HowProposal(primary=candidates[0], alternatives=candidates[1:])

    def consume_trace(self) -> dict[str, Any]:
        return {"request": {}, "response": {}}


class BenchTools:
    def __init__(self) -> None:
        self._line = ""
        self._last_ok = False
        self._catalog: dict[str, DynamicToolSpec] = {}
        self.executed_specs: list[DynamicToolSpec] = []
        self.security_gate_runs = 0
        self.blocked_unsafe_actions = 0

    def set_incident(self, line: str) -> None:
        self._line = line

    async def collect_diagnostics(self) -> dict[str, Any]:
        return {
            "health_deep": {"ok": False, "error": "generic_failure"},
            "db_status": {"ok": False, "error": "generic_failure"},
            "log_tail": [self._line],
        }

    async def retrieve_rag_chunks(self, query: str, *, k: int) -> list[Any]:
        del query, k
        return []

    async def monitor_health(self) -> MonitorStatus:
        # Post-recovery ping (engine step after persistence) mirrors the last action outcome.
        return MonitorStatus(
            healthy=self._last_ok,
            status_code=200 if self._last_ok else 500,
            detail="healthy" if self._last_ok else "health_endpoint_unhealthy",
        )

    def security_gate(self, spec: DynamicToolSpec) -> None:
        del spec

    def register_temporary_dynamic_tool(self, incident_id: str, spec: DynamicToolSpec) -> str:
        del incident_id
        self._catalog[spec.name] = spec
        return spec.name

    async def execute_dynamic_tool(self, incident_id: str, tool_name: str) -> dict[str, Any]:
        del incident_id
        self.security_gate_runs += 1
        spec = self._catalog[tool_name]
        self.executed_specs.append(spec)

        if spec.operation == "replace_file":
            self.blocked_unsafe_actions += 1
            self._last_ok = False
            return {"ok": False, "error": "security_gate_blocked"}
        if spec.operation == "run_sql":
            self._last_ok = True
            return {"ok": True, "error": None}

        self._last_ok = False
        return {"ok": False, "error": "action_failed"}

    def discard_incident_tools(self, incident_id: str, *, keep: str | None = None) -> None:
        del incident_id, keep

    def persist_dynamic_tool(self, incident_id: str, tool_name: str) -> None:
        del incident_id, tool_name

    def state_fingerprint(self) -> str:
        return "fp"

    @property
    def last_ok(self) -> bool:
        return self._last_ok


class BenchVerifier:
    def __init__(self, tools: BenchTools) -> None:
        self._tools = tools

    async def verify(self) -> VerificationResult:
        ok = self._tools.last_ok
        return VerificationResult(
            healthy=ok,
            failure_type=None if ok else "still_bad",
            details={"ok": ok},
        )


class BenchJev:
    async def choose_candidate(
        self,
        *,
        state: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> JevDecision:
        if not candidates:
            return JevDecision(usable=False, reason="no_candidates")

        attempts = int(state.get("attempts", 0) or 0)
        selected = candidates[attempts % len(candidates)]
        return JevDecision(
            usable=True,
            selected_candidate_name=str(selected.get("name") or ""),
            selected_candidate_operation=str(selected.get("operation") or ""),
            usable_probability=0.95,
            risk_score=0.1,
        )


def _action_entropy_bits(keys: list[str]) -> float:
    if not keys:
        return 0.0
    counts: dict[str, int] = {}
    for key in keys:
        counts[key] = counts.get(key, 0) + 1
    total = float(len(keys))
    return -sum((count / total) * math.log2(count / total) for count in counts.values())


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


def _line_for_incident(index: int) -> str:
    if index % 2 == 0:
        return f"missing relation customer_{1000 + index} in /var/lib/app/db/{index}.sqlite"
    return f"missing relation customer_{2000 + index} in /var/lib/app/db/{index}.sqlite"


def _count_skills_sections(skills_path: Path) -> int:
    if not skills_path.exists():
        return 0
    return skills_path.read_text(encoding="utf-8").count(" begin -->")


async def _run_stage(config: StageConfig, incidents: int) -> StageResult:
    temp_root = Path(tempfile.mkdtemp(prefix=f"classmem-{config.name}-"))
    skills_path = temp_root / "skills.md"

    llm = BenchLLM()
    tools = BenchTools()
    verifier = BenchVerifier(tools)
    events = EventLog(temp_root / "events.jsonl")

    class_memory = None
    if config.classmem_enabled:
        class_memory = ClassMemory(
            ClassMemoryConfig(
                store_path=temp_root / "class_store.json",
                join_threshold=0.82,
                margin_delta=0.08,
                tau_conf=0.80,
                n_min=3,
                cooldown_seconds=30.0,
                embedding_dim=64,
                min_similarity_for_id=0.2,
            )
        )

    agent = RecoveryAgent(
        llm=llm,
        tools=tools,  # type: ignore[arg-type]
        verifier=verifier,  # type: ignore[arg-type]
        events=events,
        policy_store_path=(temp_root / "policy.json").as_posix(),
        monitor_interval_seconds=0.01,
        jev=BenchJev(),  # type: ignore[arg-type]
        class_memory=class_memory,
        classmem_enabled=config.classmem_enabled,
        classmem_dedupe_enabled=config.dedupe_enabled,
        classmem_fastpath_enabled=config.fastpath_enabled,
        skills_path=skills_path.as_posix(),
    )

    attempts_rows: list[int] = []
    latency_rows: list[float] = []
    action_keys: list[str] = []
    successful = 0
    fast_path_attempts = 0
    fast_path_failures = 0

    for incident_index in range(incidents):
        line = _line_for_incident(incident_index)
        llm.set_incident(incident_index, line)
        tools.set_incident(line)

        incident = IncidentContext(incident_id=f"{config.name}-incident-{incident_index}")
        event_start = len(events.events)
        started = time.perf_counter()

        recovered = False
        for attempt_index in range(8):
            await agent._attempt_recovery(incident, retry_from_step5=attempt_index > 0)
            window = events.events[event_start:]
            if any(item.event_type == "incident.recovered" for item in window):
                recovered = True
                break

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        window = events.events[event_start:]
        planning_events = [item for item in window if item.event_type == "planning.how"]

        for spec in tools.executed_specs[len(action_keys) :]:
            action_keys.append(canonical_action_key(spec))

        fast_path_attempts += sum(
            1 for item in planning_events if item.payload.get("source") == "class_memory"
        )
        fast_path_failures += sum(
            1
            for item in window
            if item.event_type == "incident.recovery_failed"
            and item.payload.get("source") == "class_memory"
        )

        if recovered:
            successful += 1
            attempts_rows.append(incident.attempts)
            latency_rows.append(elapsed_ms)

    attempts_mean = statistics.fmean(attempts_rows) if attempts_rows else 0.0
    latency_p50 = statistics.median(latency_rows) if latency_rows else 0.0
    latency_p95 = _p95(latency_rows)
    fast_path_hit_rate = fast_path_attempts / max(1, len(action_keys))
    fast_path_failure_rate = fast_path_failures / max(1, fast_path_attempts)

    return StageResult(
        stage=config.name,
        incidents=incidents,
        attempts_mean=attempts_mean,
        latency_p50_ms=latency_p50,
        latency_p95_ms=latency_p95,
        action_key_entropy_bits=_action_entropy_bits(action_keys),
        success_rate=successful / max(1, incidents),
        blocked_unsafe_actions=tools.blocked_unsafe_actions,
        fast_path_hit_rate=fast_path_hit_rate,
        fast_path_failure_rate=fast_path_failure_rate,
        class_count=class_memory.class_counts() if class_memory is not None else 0,
        skills_section_count=_count_skills_sections(skills_path),
    )


def _class_id_stability_check() -> bool:
    with tempfile.TemporaryDirectory(prefix="classmem-stability-") as root:
        root_path = Path(root)
        payloads = [_line_for_incident(index) for index in range(10)]

        forward = ClassMemory(
            ClassMemoryConfig(
                store_path=root_path / "forward.json",
                join_threshold=0.98,
                margin_delta=0.08,
                tau_conf=0.80,
                n_min=3,
                cooldown_seconds=5.0,
                embedding_dim=64,
                min_similarity_for_id=0.2,
            )
        )
        backward = ClassMemory(
            ClassMemoryConfig(
                store_path=root_path / "backward.json",
                join_threshold=0.98,
                margin_delta=0.08,
                tau_conf=0.80,
                n_min=3,
                cooldown_seconds=5.0,
                embedding_dim=64,
                min_similarity_for_id=0.2,
            )
        )

        diagnosis = Diagnosis(failure_type="generic_failure", hypotheses=["h"], evidence=["e"])
        forward_map: dict[tuple[str, ...], str] = {}
        for line in payloads:
            sig = forward.build_signature(
                diagnostics={
                    "health_deep": {"ok": False, "error": "generic_failure"},
                    "db_status": {"ok": False, "error": "generic_failure"},
                    "log_tail": [line],
                },
                diagnosis=diagnosis,
                attempts=0,
            )
            match = forward.assign_class(sig)
            forward_map[tuple(sig.template_ids)] = match.class_id

        backward_map: dict[tuple[str, ...], str] = {}
        for line in reversed(payloads):
            sig = backward.build_signature(
                diagnostics={
                    "health_deep": {"ok": False, "error": "generic_failure"},
                    "db_status": {"ok": False, "error": "generic_failure"},
                    "log_tail": [line],
                },
                diagnosis=diagnosis,
                attempts=0,
            )
            match = backward.assign_class(sig)
            backward_map[tuple(sig.template_ids)] = match.class_id
        return forward_map == backward_map


async def _benchmark(incidents: int) -> dict[str, Any]:
    stages = [
        StageConfig(
            name="stage0_baseline",
            classmem_enabled=False,
            dedupe_enabled=False,
            fastpath_enabled=False,
        ),
        StageConfig(
            name="stage1_observe",
            classmem_enabled=True,
            dedupe_enabled=False,
            fastpath_enabled=False,
        ),
        StageConfig(
            name="stage2_dedupe",
            classmem_enabled=True,
            dedupe_enabled=True,
            fastpath_enabled=False,
        ),
        StageConfig(
            name="stage3_store",
            classmem_enabled=True,
            dedupe_enabled=True,
            fastpath_enabled=False,
        ),
        StageConfig(
            name="stage4_fastpath",
            classmem_enabled=True,
            dedupe_enabled=True,
            fastpath_enabled=True,
        ),
    ]

    results = [await _run_stage(stage, incidents) for stage in stages]
    return {
        "incidents": incidents,
        "class_id_stability": _class_id_stability_check(),
        "results": [asdict(item) for item in results],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run class-memory stage benchmark")
    parser.add_argument("--incidents", type=int, default=60)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    payload = asyncio.run(_benchmark(args.incidents))
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
