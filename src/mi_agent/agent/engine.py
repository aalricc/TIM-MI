from __future__ import annotations

import asyncio
import copy
import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from mi_agent.class_memory import (
    ClassMatch,
    ClassMemory,
    FastPathDecision,
    OutcomeUpdate,
    canonical_action_key,
    dedupe_candidates_by_action_key,
)
from mi_agent.decision.client import JevDecision, JevDecisionClient
from mi_agent.errors import SecurityGateError
from mi_agent.events import EventLog
from mi_agent.llm.client import LLMClient
from mi_agent.observability import sanitize_payload
from mi_agent.rag_query import (
    build_repair_query,
    build_symptom_query,
    cap_chunks_per_source,
    prepare_retrieval_query,
)
from mi_agent.tools.executor import ToolExecutor, base_tool_name
from mi_agent.verifier.verifier import Verifier
from mi_agent.workflow.models import (
    AttemptFeedback,
    DynamicToolSpec,
    HowProposal,
    IncidentContext,
    MonitorStatus,
    RAGChunk,
)
from mi_agent.workflow.state_machine import WorkflowEvent, WorkflowState, transition


@dataclass(slots=True)
class AgentSnapshot:
    workflow_state: WorkflowState
    incident: IncidentContext | None
    last_monitor: MonitorStatus | None


class RecoveryAgent:
    def __init__(
        self,
        *,
        llm: LLMClient,
        tools: ToolExecutor,
        verifier: Verifier,
        events: EventLog,
        policy_store_path: str,
        monitor_interval_seconds: float,
        skills_path: str | None = None,
        jev: JevDecisionClient | None = None,
        class_memory: ClassMemory | None = None,
        classmem_enabled: bool = False,
        classmem_dedupe_enabled: bool = True,
        classmem_fastpath_enabled: bool = True,
        rag_top_k: int = 5,
        rag_fetch_depth: int = 10,
        rag_query_max_chars: int = 300,
        rag_query_prefix: str = "sqlite",
        rag_query_rewrite: bool = True,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._verifier = verifier
        self._events = events
        self._policy_store_path = policy_store_path
        self._monitor_interval_seconds = monitor_interval_seconds
        self._jev = jev
        self._class_memory = class_memory
        self._classmem_enabled = classmem_enabled and class_memory is not None
        self._classmem_dedupe_enabled = classmem_dedupe_enabled
        self._classmem_fastpath_enabled = classmem_fastpath_enabled
        self._rag_top_k = max(1, rag_top_k)
        self._rag_fetch_depth = max(1, rag_fetch_depth)
        self._rag_query_max_chars = max(120, rag_query_max_chars)
        self._rag_query_prefix = rag_query_prefix
        self._rag_query_rewrite = rag_query_rewrite
        default_skills_path = str(Path(policy_store_path).with_name("skills.md"))
        self._skills_path = skills_path or default_skills_path

        self._workflow_state = WorkflowState.HEALTHY
        self._incident: IncidentContext | None = None
        self._last_monitor: MonitorStatus | None = None
        self._policy: dict[str, dict[str, float]] = self._load_policy()
        self._llm_traces: dict[str, dict[str, Any]] = {}
        self._llm_trace_order: list[str] = []
        self._max_llm_trace_incidents = 120

    @staticmethod
    def _path_mode_for_source(source: str) -> str:
        if source == "class_memory":
            return "fast_path"
        return "full_loop"

    async def _publish_workflow_step(
        self,
        *,
        incident_id: str,
        attempt: int,
        step: str,
        path_mode: str,
        status: str = "ok",
        **extra: Any,
    ) -> None:
        payload: dict[str, Any] = {
            "incident_id": incident_id,
            "attempt": attempt,
            "path_mode": path_mode,
            "step": step,
            "status": status,
        }
        payload.update(extra)
        await self._events.publish("workflow.step", payload)

    @property
    def snapshot(self) -> AgentSnapshot:
        return AgentSnapshot(
            workflow_state=self._workflow_state,
            incident=self._incident,
            last_monitor=self._last_monitor,
        )

    @property
    def policy(self) -> dict[str, dict[str, float]]:
        return copy.deepcopy(self._policy)

    def get_llm_trace(self, incident_id: str | None = None) -> dict[str, Any] | None:
        if incident_id is None:
            if not self._llm_trace_order:
                return None
            incident_id = self._llm_trace_order[-1]
        trace = self._llm_traces.get(incident_id)
        if trace is None:
            return None
        return copy.deepcopy(trace)

    def recent_llm_trace_ids(self, limit: int = 20) -> list[str]:
        safe_limit = max(1, min(limit, 200))
        return list(reversed(self._llm_trace_order[-safe_limit:]))

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_iteration()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._events.publish(
                    "agent.loop_error",
                    {
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
            await asyncio.sleep(self._monitor_interval_seconds)

    async def run_iteration(self) -> None:
        if (
            self._incident is not None
            and self._workflow_state == WorkflowState.INCIDENT_ACTIVE
            and self._incident.attempts > 0
        ):
            await self._attempt_recovery(self._incident, retry_from_step5=True)
            return

        monitor = await self._tools.monitor_health()
        self._last_monitor = monitor
        await self._events.publish("monitor.health", monitor.model_dump())

        if monitor.healthy:
            self._workflow_state = transition(self._workflow_state, WorkflowEvent.STABLE)
            if self._incident is not None:
                await self._events.publish(
                    "incident.closed",
                    {
                        "incident_id": self._incident.incident_id,
                        "attempts": self._incident.attempts,
                    },
                )
                self._incident = None
            return

        if self._incident is None:
            self._incident = IncidentContext(incident_id=f"inc-{uuid.uuid4().hex[:10]}")
            await self._events.publish(
                "incident.started",
                {"incident_id": self._incident.incident_id},
            )
            await self._publish_workflow_step(
                incident_id=self._incident.incident_id,
                attempt=1,
                path_mode="monitor",
                step="monitor.unhealthy_detected",
                status="detected",
                detail=monitor.detail,
            )
        self._workflow_state = transition(self._workflow_state, WorkflowEvent.UNHEALTHY_DETECTED)

        await self._attempt_recovery(self._incident, retry_from_step5=False)

    async def _attempt_recovery(
        self,
        incident: IncidentContext,
        *,
        retry_from_step5: bool,
    ) -> None:
        attempt = incident.attempts + 1
        diagnostics = incident.latest_diagnostics
        if retry_from_step5 and incident.diagnosis is not None and diagnostics:
            diagnosis = incident.diagnosis
            await self._publish_workflow_step(
                incident_id=incident.incident_id,
                attempt=attempt,
                path_mode="full_loop",
                step="context.reused",
                status="ok",
                from_step=5,
            )
        else:
            diagnostics = await self._tools.collect_diagnostics()
            incident.latest_diagnostics = diagnostics
            await self._publish_workflow_step(
                incident_id=incident.incident_id,
                attempt=attempt,
                path_mode="full_loop",
                step="diagnostics.collected",
                status="ok",
            )
            symptom_query = self._build_symptom_query(diagnostics)
            incident.rag_symptom_chunks = await self._retrieve_rag_chunks_for_incident(
                incident,
                phase="symptom",
                query=symptom_query,
                attempt=attempt,
                path_mode="full_loop",
            )

            diagnosis = await self._llm.diagnose_why(
                evidence=diagnostics,
                rag_chunks=incident.rag_symptom_chunks,
            )
            await self._publish_workflow_step(
                incident_id=incident.incident_id,
                attempt=attempt,
                path_mode="full_loop",
                step="llm.why",
                status="ok",
                failure_type=diagnosis.failure_type,
            )
            incident.diagnosis = diagnosis
            self._record_diagnosis_trace(
                incident_id=incident.incident_id,
                diagnosis=diagnosis,
                rag_status=incident.rag_symptom_status,
            )
            await self._events.publish(
                "diagnosis.why",
                {
                    "incident_id": incident.incident_id,
                    **diagnosis.model_dump(),
                },
            )

        class_match: ClassMatch | None = None
        fast_path_decision: FastPathDecision | None = None
        full_loop_reason = "class_memory_disabled"
        if self._classmem_enabled and self._class_memory is not None:
            signature = self._class_memory.build_signature(
                diagnostics=diagnostics,
                diagnosis=diagnosis,
                attempts=incident.attempts,
            )
            class_match = self._class_memory.assign_class(signature)
            fast_path_decision = self._class_memory.choose_fast_path(
                match=class_match,
                failed_action_keys=incident.failed_action_keys,
            )

            await self._events.publish(
                "class_memory.decision",
                {
                    "incident_id": incident.incident_id,
                    "class_id": class_match.class_id,
                    "s1": class_match.s1,
                    "s2": class_match.s2,
                    "margin": class_match.margin,
                    "fast_path_eligible": fast_path_decision.eligible,
                    "first_failing_gate_reason": fast_path_decision.gate_reason,
                    "chosen_action_key": fast_path_decision.action_key,
                    "source": fast_path_decision.source,
                    "new_class": class_match.created_new,
                },
            )
            full_loop_reason = str(fast_path_decision.gate_reason or "fast_path_not_eligible")
            await self._publish_workflow_step(
                incident_id=incident.incident_id,
                attempt=attempt,
                path_mode="class_memory",
                step="class_memory.lookup",
                status="ok",
                class_id=class_match.class_id,
                fast_path_eligible=fast_path_decision.eligible,
                gate_reason=fast_path_decision.gate_reason,
            )
        else:
            await self._publish_workflow_step(
                incident_id=incident.incident_id,
                attempt=attempt,
                path_mode="class_memory",
                step="class_memory.lookup",
                status="disabled",
                reason="class_memory_disabled",
            )

        if (
            self._classmem_enabled
            and self._classmem_fastpath_enabled
            and fast_path_decision is not None
            and fast_path_decision.eligible
            and fast_path_decision.candidate is not None
        ):
            await self._publish_workflow_step(
                incident_id=incident.incident_id,
                attempt=attempt,
                path_mode="fast_path",
                step="path.selected",
                status="ok",
                reason="fast_path_eligible",
                class_id=class_match.class_id if class_match is not None else None,
            )
            fast_path_context: dict[str, Any] = {
                "class_id": class_match.class_id if class_match is not None else None,
                "action_key": fast_path_decision.action_key,
                "gate_reason": fast_path_decision.gate_reason,
                "s1": fast_path_decision.s1,
                "s2": fast_path_decision.s2,
                "margin": fast_path_decision.margin,
            }
            if (
                self._class_memory is not None
                and class_match is not None
                and fast_path_decision.action_key is not None
            ):
                stats = self._class_memory.action_stats(
                    class_id=class_match.class_id,
                    action_key=fast_path_decision.action_key,
                )
                if stats is not None:
                    fast_path_context["champion_stats"] = stats

            fast_how = HowProposal(primary=fast_path_decision.candidate, alternatives=[])
            selected_how, executed_candidate, jev_decision = await self._select_candidate_with_jev(
                incident=incident,
                diagnosis=diagnosis,
                how=fast_how,
                source="class_memory",
                attempt=attempt,
                count_unusable_failure=False,
                class_match=class_match,
                fast_path_context=fast_path_context,
            )
            if selected_how is not None:
                await self._execute_selected_action(
                    incident=incident,
                    how=fast_how,
                    selected_how=selected_how,
                    executed_candidate=executed_candidate,
                    jev_decision=jev_decision,
                    source="class_memory",
                    attempt=attempt,
                    class_match=class_match,
                    selected_action_key=fast_path_decision.action_key,
                )
                return
            full_loop_reason = "fast_path_unusable_fallback"

        await self._publish_workflow_step(
            incident_id=incident.incident_id,
            attempt=attempt,
            path_mode="full_loop",
            step="path.selected",
            status="ok",
            reason=full_loop_reason,
        )

        repair_query = self._build_repair_query(incident)
        incident.rag_repair_chunks = await self._retrieve_rag_chunks_for_incident(
            incident,
            phase="repair",
            query=repair_query,
            attempt=attempt,
            path_mode="full_loop",
        )
        incident.rag_repair_chunks.extend(self._load_skills_chunks())

        raw_how = await self._llm.propose_how(
            diagnosis=diagnosis,
            rag_chunks=incident.rag_repair_chunks,
            prior_attempts=incident.prior_attempts,
            latest_tool_error=incident.last_tool_error,
            state_fingerprint=incident.state_fingerprint,
        )
        how = self._normalize_how_proposal(raw_how)
        await self._publish_workflow_step(
            incident_id=incident.incident_id,
            attempt=attempt,
            path_mode="full_loop",
            step="llm.how",
            status="ok",
            candidate_count=len([how.primary, *how.alternatives]),
        )

        selected_how, executed_candidate, jev_decision = await self._select_candidate_with_jev(
            incident=incident,
            diagnosis=diagnosis,
            how=how,
            source="full_loop",
            attempt=attempt,
            class_match=class_match,
        )
        if selected_how is None:
            return

        await self._execute_selected_action(
            incident=incident,
            how=how,
            selected_how=selected_how,
            executed_candidate=executed_candidate,
            jev_decision=jev_decision,
            source="full_loop",
            attempt=attempt,
            class_match=class_match,
            selected_action_key=None,
        )

    async def _execute_selected_action(
        self,
        *,
        incident: IncidentContext,
        how: HowProposal,
        selected_how: DynamicToolSpec,
        executed_candidate: str,
        jev_decision: dict[str, Any] | None,
        source: str,
        attempt: int,
        class_match: ClassMatch | None,
        selected_action_key: str | None,
    ) -> None:
        started = time.perf_counter()
        action_key = selected_action_key or canonical_action_key(selected_how)
        path_mode = self._path_mode_for_source(source)
        try:
            self._tools.security_gate(selected_how)
        except (SecurityGateError, ValidationError) as exc:
            await self._publish_workflow_step(
                incident_id=incident.incident_id,
                attempt=attempt,
                path_mode=path_mode,
                step="security.gate",
                status="rejected",
                operation=selected_how.operation,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            await self._handle_gate_rejection(
                incident=incident,
                selected_how=selected_how,
                error=exc,
                source=source,
                attempt=attempt,
                class_match=class_match,
                action_key=action_key,
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
            return
        await self._publish_workflow_step(
            incident_id=incident.incident_id,
            attempt=attempt,
            path_mode=path_mode,
            step="security.gate",
            status="ok",
            operation=selected_how.operation,
        )
        tool_name = self._tools.register_temporary_dynamic_tool(incident.incident_id, selected_how)
        await self._publish_workflow_step(
            incident_id=incident.incident_id,
            attempt=attempt,
            path_mode=path_mode,
            step="dynamic_tool.registered",
            status="ok",
            tool_name=tool_name,
            operation=selected_how.operation,
        )
        self._record_how_trace(
            incident_id=incident.incident_id,
            proposal=how,
            executed_tool_name=tool_name,
            executed_candidate=executed_candidate,
            jev_decision=jev_decision,
            source=source,
            action_key=action_key,
            class_id=class_match.class_id if class_match is not None else None,
            rag_symptom_status=incident.rag_symptom_status,
            rag_repair_status=incident.rag_repair_status,
        )
        planning_payload: dict[str, Any] = {
            "incident_id": incident.incident_id,
            "executed_tool_name": tool_name,
            "primary": how.primary.model_dump(),
            "alternatives": [item.model_dump() for item in how.alternatives],
            "executed_candidate": executed_candidate,
            "jev_decision": jev_decision,
        }
        if self._classmem_enabled:
            planning_payload.update(
                {
                    "source": source,
                    "action_key": action_key,
                    "class_id": class_match.class_id if class_match is not None else None,
                }
            )
        await self._events.publish(
            "planning.how",
            planning_payload,
        )

        action_result = await self._tools.execute_dynamic_tool(incident.incident_id, tool_name)
        await self._publish_workflow_step(
            incident_id=incident.incident_id,
            attempt=attempt,
            path_mode=path_mode,
            step="dynamic_tool.executed",
            status="ok" if bool(action_result.get("ok", False)) else "error",
            tool_name=tool_name,
            operation=selected_how.operation,
            error=action_result.get("error"),
        )
        executed_payload: dict[str, Any] = {
            "incident_id": incident.incident_id,
            "tool_name": tool_name,
            "operation": selected_how.operation,
            "ok": bool(action_result.get("ok", False)),
            "error": action_result.get("error"),
            "result": action_result,
        }
        if self._classmem_enabled:
            executed_payload.update(
                {
                    "source": source,
                    "action_key": action_key,
                }
            )
        await self._events.publish(
            "dynamic_tool.executed",
            executed_payload,
        )

        verification = await self._verifier.verify()
        await self._publish_workflow_step(
            incident_id=incident.incident_id,
            attempt=attempt,
            path_mode=path_mode,
            step="verification.result",
            status="ok" if verification.healthy else "failed",
            failure_type=verification.failure_type,
        )
        verification_payload: dict[str, Any] = {
            "incident_id": incident.incident_id,
            **verification.model_dump(),
        }
        if self._classmem_enabled:
            verification_payload.update(
                {
                    "source": source,
                    "action_key": action_key,
                }
            )
        await self._events.publish(
            "verification.result",
            verification_payload,
        )

        incident.attempts += 1
        incident.state_fingerprint = self._tools.state_fingerprint()
        feedback = AttemptFeedback(
            tool_name=tool_name,
            operation=selected_how.operation,
            tool_error=(
                None
                if bool(action_result.get("ok", False))
                else str(action_result.get("error"))
            ),
            verifier_details=verification.details,
        )
        incident.prior_attempts.append(feedback)

        latency_ms = (time.perf_counter() - started) * 1000.0

        if verification.healthy:
            outcome_ts = time.time()
            self._tools.persist_dynamic_tool(incident.incident_id, tool_name)
            self._tools.discard_incident_tools(incident.incident_id, keep=tool_name)
            await self._publish_workflow_step(
                incident_id=incident.incident_id,
                attempt=attempt,
                path_mode=path_mode,
                step="persist.tool_catalog",
                status="ok",
                tool_name=tool_name,
            )

            self._update_policy(tool_name, reward=1.0)
            await self._publish_workflow_step(
                incident_id=incident.incident_id,
                attempt=attempt,
                path_mode=path_mode,
                step="persist.policy_store",
                status="ok",
                action_key=tool_name,
                reward=1.0,
            )

            if (
                self._classmem_enabled
                and self._class_memory is not None
                and class_match is not None
            ):
                # Render with the pending success so a new class gets its skill on the
                # first recovery, before step 16 persists the same outcome.
                section = self._class_memory.render_skills_section(
                    class_id=class_match.class_id,
                    pending=OutcomeUpdate(
                        match=class_match,
                        spec=selected_how,
                        latency_ms=latency_ms,
                        success=True,
                        source=source,
                        ts=outcome_ts,
                    ),
                )
                if section:
                    self._class_memory.upsert_skills_section(
                        Path(self._skills_path),
                        class_id=class_match.class_id,
                        section_body=section,
                    )
                    await self._publish_workflow_step(
                        incident_id=incident.incident_id,
                        attempt=attempt,
                        path_mode=path_mode,
                        step="persist.skills",
                        status="ok",
                        class_id=class_match.class_id,
                    )
                else:
                    await self._publish_workflow_step(
                        incident_id=incident.incident_id,
                        attempt=attempt,
                        path_mode=path_mode,
                        step="persist.skills",
                        status="skipped",
                        reason="empty_class_memory_section",
                        class_id=class_match.class_id,
                    )
            else:
                self._append_skill_entry(
                    incident=incident,
                    tool_name=tool_name,
                    operation=selected_how.operation,
                )
                await self._publish_workflow_step(
                    incident_id=incident.incident_id,
                    attempt=attempt,
                    path_mode=path_mode,
                    step="persist.skills",
                    status="ok",
                )

            if (
                self._classmem_enabled
                and self._class_memory is not None
                and class_match is not None
            ):
                self._class_memory.record_outcome(
                    match=class_match,
                    spec=selected_how,
                    latency_ms=latency_ms,
                    success=True,
                    source=source,
                    ts=outcome_ts,
                )
                await self._publish_workflow_step(
                    incident_id=incident.incident_id,
                    attempt=attempt,
                    path_mode=path_mode,
                    step="persist.class_memory",
                    status="ok",
                    class_id=class_match.class_id,
                )
            else:
                reason = "class_memory_disabled"
                if self._classmem_enabled and class_match is None:
                    reason = "class_match_unavailable"
                await self._publish_workflow_step(
                    incident_id=incident.incident_id,
                    attempt=attempt,
                    path_mode=path_mode,
                    step="persist.class_memory",
                    status="skipped",
                    reason=reason,
                )

            post_recovery_monitor = await self._tools.monitor_health()
            self._last_monitor = post_recovery_monitor
            await self._events.publish("monitor.health", post_recovery_monitor.model_dump())
            await self._publish_workflow_step(
                incident_id=incident.incident_id,
                attempt=attempt,
                path_mode=path_mode,
                step="post_recovery.ping",
                status="healthy" if post_recovery_monitor.healthy else "unhealthy",
                detail=post_recovery_monitor.detail,
                status_code=post_recovery_monitor.status_code,
            )

            if post_recovery_monitor.healthy:
                self._workflow_state = transition(
                    self._workflow_state,
                    WorkflowEvent.RECOVERY_SUCCEEDED,
                )
                recovered_payload: dict[str, Any] = {
                    "incident_id": incident.incident_id,
                    "tool_name": tool_name,
                    "attempts": incident.attempts,
                }
                if self._classmem_enabled:
                    recovered_payload.update(
                        {
                            "latency_ms": latency_ms,
                            "source": source,
                            "action_key": action_key,
                            "class_id": class_match.class_id if class_match is not None else None,
                        }
                    )
                await self._events.publish(
                    "incident.recovered",
                    recovered_payload,
                )
                await self._publish_workflow_step(
                    incident_id=incident.incident_id,
                    attempt=attempt,
                    path_mode=path_mode,
                    step="attempt.result",
                    status="recovered",
                    tool_name=tool_name,
                )
                await self._events.publish(
                    "incident.closed",
                    {
                        "incident_id": incident.incident_id,
                        "attempts": incident.attempts,
                    },
                )
                self._incident = None
                return

            incident.last_tool_error = "post_recovery_ping_unhealthy"
            if self._classmem_enabled:
                incident.failed_action_keys.add(action_key)
            self._workflow_state = transition(self._workflow_state, WorkflowEvent.RECOVERY_FAILED)
            await self._publish_workflow_step(
                incident_id=incident.incident_id,
                attempt=attempt,
                path_mode=path_mode,
                step="attempt.result",
                status="retry",
                tool_name=tool_name,
                reason=incident.last_tool_error,
            )
            return

        incident.last_tool_error = str(
            action_result.get("error") or verification.failure_type or "unknown"
        )
        if self._classmem_enabled:
            incident.failed_action_keys.add(action_key)
        self._tools.discard_incident_tools(incident.incident_id)
        self._update_policy(tool_name, reward=-1.0)
        await self._publish_workflow_step(
            incident_id=incident.incident_id,
            attempt=attempt,
            path_mode=path_mode,
            step="persist.policy_store",
            status="failure",
            action_key=tool_name,
            reward=-1.0,
        )
        if self._classmem_enabled and self._class_memory is not None and class_match is not None:
            self._class_memory.record_outcome(
                match=class_match,
                spec=selected_how,
                latency_ms=latency_ms,
                success=False,
                source=source,
            )
            await self._publish_workflow_step(
                incident_id=incident.incident_id,
                attempt=attempt,
                path_mode=path_mode,
                step="persist.class_memory",
                status="failure",
                class_id=class_match.class_id,
            )
        self._workflow_state = transition(self._workflow_state, WorkflowEvent.RECOVERY_FAILED)
        failed_payload: dict[str, Any] = {
            "incident_id": incident.incident_id,
            "tool_name": tool_name,
            "error": incident.last_tool_error,
            "attempts": incident.attempts,
        }
        if self._classmem_enabled:
            failed_payload.update(
                {
                    "latency_ms": latency_ms,
                    "source": source,
                    "action_key": action_key,
                    "class_id": class_match.class_id if class_match is not None else None,
                }
            )
        await self._events.publish(
            "incident.recovery_failed",
            failed_payload,
        )
        await self._publish_workflow_step(
            incident_id=incident.incident_id,
            attempt=attempt,
            path_mode=path_mode,
            step="attempt.result",
            status="failed",
            tool_name=tool_name,
            reason=incident.last_tool_error,
        )

    async def _handle_gate_rejection(
        self,
        *,
        incident: IncidentContext,
        selected_how: DynamicToolSpec,
        error: Exception,
        source: str,
        attempt: int,
        class_match: ClassMatch | None,
        action_key: str,
        latency_ms: float,
    ) -> None:
        """A security-gate rejection is a failed attempt: nothing executes, the action is
        excluded for this incident and penalised in learning state (the gate's verdict for
        a spec is deterministic), and the loop retries from step 5."""
        path_mode = self._path_mode_for_source(source)
        policy_key = base_tool_name(selected_how)
        incident.attempts += 1
        incident.state_fingerprint = self._tools.state_fingerprint()
        incident.last_tool_error = f"security_gate_rejected:{type(error).__name__}: {error}"
        incident.prior_attempts.append(
            AttemptFeedback(
                tool_name=policy_key,
                operation=selected_how.operation,
                tool_error=incident.last_tool_error,
                verifier_details={"security_gate": "rejected"},
            )
        )
        if self._classmem_enabled:
            incident.failed_action_keys.add(action_key)

        self._update_policy(policy_key, reward=-1.0)
        await self._publish_workflow_step(
            incident_id=incident.incident_id,
            attempt=attempt,
            path_mode=path_mode,
            step="persist.policy_store",
            status="failure",
            action_key=policy_key,
            reward=-1.0,
        )
        if self._classmem_enabled and self._class_memory is not None and class_match is not None:
            self._class_memory.record_outcome(
                match=class_match,
                spec=selected_how,
                latency_ms=latency_ms,
                success=False,
                source=source,
            )
            await self._publish_workflow_step(
                incident_id=incident.incident_id,
                attempt=attempt,
                path_mode=path_mode,
                step="persist.class_memory",
                status="failure",
                class_id=class_match.class_id,
            )

        self._workflow_state = transition(self._workflow_state, WorkflowEvent.RECOVERY_FAILED)
        failed_payload: dict[str, Any] = {
            "incident_id": incident.incident_id,
            "tool_name": policy_key,
            "error": incident.last_tool_error,
            "attempts": incident.attempts,
        }
        if self._classmem_enabled:
            failed_payload.update(
                {
                    "latency_ms": latency_ms,
                    "source": source,
                    "action_key": action_key,
                    "class_id": class_match.class_id if class_match is not None else None,
                }
            )
        await self._events.publish("incident.recovery_failed", failed_payload)
        await self._publish_workflow_step(
            incident_id=incident.incident_id,
            attempt=attempt,
            path_mode=path_mode,
            step="attempt.result",
            status="failed",
            tool_name=policy_key,
            reason=incident.last_tool_error,
        )

    async def _select_candidate_with_jev(
        self,
        *,
        incident: IncidentContext,
        diagnosis: Any,
        how: HowProposal,
        source: str,
        attempt: int,
        count_unusable_failure: bool = True,
        class_match: ClassMatch | None = None,
        fast_path_context: dict[str, Any] | None = None,
    ) -> tuple[DynamicToolSpec | None, str, dict[str, Any] | None]:
        path_mode = self._path_mode_for_source(source)
        candidates = [how.primary, *how.alternatives]
        if self._classmem_enabled and self._classmem_dedupe_enabled:
            candidates = dedupe_candidates_by_action_key(candidates)
            candidates = [
                item
                for item in candidates
                if canonical_action_key(item) not in incident.failed_action_keys
            ]
            if not candidates:
                if count_unusable_failure:
                    await self._handle_unusable_decision(
                        incident=incident,
                        reason="all_candidates_previously_failed",
                        candidate_rows=[],
                        decision_payload={
                            "usable": False,
                            "reason": "all_candidates_previously_failed",
                        },
                        attempt=attempt,
                        class_match=class_match,
                        source=source,
                    )
                elif source == "class_memory":
                    await self._publish_fast_path_skipped(
                        incident=incident,
                        attempt=attempt,
                        reason="all_candidates_previously_failed",
                        decision_payload={
                            "usable": False,
                            "reason": "all_candidates_previously_failed",
                        },
                        candidate_rows=[],
                        class_match=class_match,
                        selected_action_key=None,
                        fast_path_context=fast_path_context,
                    )
                return None, "none", {"usable": False, "reason": "all_candidates_previously_failed"}
        candidate_rows = [candidate.model_dump() for candidate in candidates]
        await self._publish_workflow_step(
            incident_id=incident.incident_id,
            attempt=attempt,
            path_mode=path_mode,
            step="jev.requested",
            status="started",
            source=source,
            candidate_count=len(candidate_rows),
        )

        if self._jev is None:
            await self._publish_workflow_step(
                incident_id=incident.incident_id,
                attempt=attempt,
                path_mode=path_mode,
                step="jev.result",
                status="unusable",
                source=source,
                reason="jev_not_configured",
            )
            if count_unusable_failure:
                await self._handle_unusable_decision(
                    incident=incident,
                    reason="jev_not_configured",
                    candidate_rows=candidate_rows,
                    decision_payload={"usable": False, "reason": "jev_not_configured"},
                    attempt=attempt,
                    class_match=class_match,
                    source=source,
                )
            elif source == "class_memory":
                await self._publish_fast_path_skipped(
                    incident=incident,
                    attempt=attempt,
                    reason="jev_not_configured",
                    decision_payload={"usable": False, "reason": "jev_not_configured"},
                    candidate_rows=candidate_rows,
                    class_match=class_match,
                    selected_action_key=None,
                    fast_path_context=fast_path_context,
                )
            return None, "none", {"usable": False, "reason": "jev_not_configured"}

        state = {
            "incident_id": incident.incident_id,
            "failure_type": diagnosis.failure_type,
            "hypotheses": diagnosis.hypotheses,
            "latest_tool_error": incident.last_tool_error,
            "state_fingerprint": incident.state_fingerprint,
            "attempts": incident.attempts,
            "prior_attempts": [attempt.model_dump() for attempt in incident.prior_attempts],
            "candidate_actions": candidate_rows,
        }
        if self._classmem_enabled:
            state["source"] = source
            if source == "class_memory" and fast_path_context is not None:
                state["class_memory_candidate"] = fast_path_context
        decision = await self._jev.choose_candidate(state=state, candidates=candidate_rows)
        if (
            source == "class_memory"
            and fast_path_context is not None
            and not decision.usable
            and isinstance(decision.reason, str)
            and decision.reason.startswith("jev_request_error")
            and "class_memory_candidate" in state
        ):
            fallback_state = dict(state)
            fallback_state.pop("class_memory_candidate", None)
            decision = await self._jev.choose_candidate(
                state=fallback_state,
                candidates=candidate_rows,
            )
            if (
                not decision.usable
                and isinstance(decision.reason, str)
                and decision.reason.startswith("jev_request_error")
                and "source" in fallback_state
            ):
                strict_state = dict(fallback_state)
                strict_state.pop("source", None)
                decision = await self._jev.choose_candidate(
                    state=strict_state,
                    candidates=candidate_rows,
                )
        decision_payload = self._decision_payload(decision)
        await self._publish_workflow_step(
            incident_id=incident.incident_id,
            attempt=attempt,
            path_mode=path_mode,
            step="jev.result",
            status="usable" if decision.usable else "unusable",
            source=source,
            reason=decision.reason,
            selected_candidate=decision.selected_candidate_name,
            selected_operation=decision.selected_candidate_operation,
        )
        if not decision.usable:
            if count_unusable_failure:
                await self._handle_unusable_decision(
                    incident=incident,
                    reason=decision.reason or "jev_decision_unusable",
                    candidate_rows=candidate_rows,
                    decision_payload=decision_payload,
                    attempt=attempt,
                    class_match=class_match,
                    source=source,
                )
            elif source == "class_memory":
                selected_spec = self._selected_candidate_from_decision(
                    candidate_rows=candidate_rows,
                    decision_payload=decision_payload,
                )
                selected_action_key = None
                if self._classmem_enabled and selected_spec is not None:
                    selected_action_key = canonical_action_key(selected_spec)
                    incident.failed_action_keys.add(selected_action_key)

                await self._publish_fast_path_skipped(
                    incident=incident,
                    attempt=attempt,
                    reason=decision.reason or "jev_decision_unusable",
                    decision_payload=decision_payload,
                    candidate_rows=candidate_rows,
                    class_match=class_match,
                    selected_action_key=selected_action_key,
                    fast_path_context=fast_path_context,
                )
            return None, "none", decision_payload

        selected = self._pick_candidate(candidates, decision)
        if selected is None:
            if count_unusable_failure:
                await self._handle_unusable_decision(
                    incident=incident,
                    reason="selected_candidate_not_found",
                    candidate_rows=candidate_rows,
                    decision_payload=decision_payload,
                    attempt=attempt,
                    class_match=class_match,
                    source=source,
                )
            elif source == "class_memory":
                await self._publish_fast_path_skipped(
                    incident=incident,
                    attempt=attempt,
                    reason="selected_candidate_not_found",
                    decision_payload=decision_payload,
                    candidate_rows=candidate_rows,
                    class_match=class_match,
                    selected_action_key=None,
                    fast_path_context=fast_path_context,
                )
            return None, "none", decision_payload

        if selected == how.primary:
            executed_candidate = "primary"
        else:
            executed_candidate = f"alternative:{selected.name}"
        await self._events.publish(
            "decision.selected",
            {
                "incident_id": incident.incident_id,
                "selected_candidate": selected.name,
                "selected_operation": selected.operation,
                "executed_candidate": executed_candidate,
                "decision": decision_payload,
            },
        )
        return selected, executed_candidate, decision_payload

    @staticmethod
    def _pick_candidate(
        candidates: list[DynamicToolSpec],
        decision: JevDecision,
    ) -> DynamicToolSpec | None:
        if decision.selected_candidate_name is None:
            return None
        for candidate in candidates:
            if candidate.name == decision.selected_candidate_name:
                return candidate
        if decision.selected_candidate_operation is not None:
            for candidate in candidates:
                if candidate.operation == decision.selected_candidate_operation:
                    return candidate
        return None

    async def _handle_unusable_decision(
        self,
        *,
        incident: IncidentContext,
        reason: str,
        candidate_rows: list[dict[str, Any]],
        decision_payload: dict[str, Any],
        attempt: int,
        class_match: ClassMatch | None,
        source: str,
    ) -> None:
        path_mode = self._path_mode_for_source(source)
        selected_spec = self._selected_candidate_from_decision(
            candidate_rows=candidate_rows,
            decision_payload=decision_payload,
        )
        selected_action_key: str | None = None
        transport_error = reason.startswith("jev_request_error")
        if selected_spec is not None and self._classmem_enabled:
            selected_action_key = canonical_action_key(selected_spec)
            incident.failed_action_keys.add(selected_action_key)

        incident.attempts += 1
        incident.state_fingerprint = self._tools.state_fingerprint()
        incident.last_tool_error = reason
        incident.prior_attempts.append(
            AttemptFeedback(
                tool_name="decision_unusable",
                operation="jev_decision",
                tool_error=reason,
                verifier_details={
                    "candidate_actions": candidate_rows,
                    "decision": decision_payload,
                    "selected_action_key": selected_action_key,
                },
            )
        )
        if not transport_error:
            self._update_policy("decision_unusable", reward=-1.0)
            await self._publish_workflow_step(
                incident_id=incident.incident_id,
                attempt=attempt,
                path_mode=path_mode,
                step="persist.policy_store",
                status="failure",
                action_key="decision_unusable",
                reward=-1.0,
            )
        if (
            selected_spec is not None
            and self._classmem_enabled
            and self._class_memory is not None
            and class_match is not None
            and not transport_error
        ):
            self._class_memory.record_outcome(
                match=class_match,
                spec=selected_spec,
                latency_ms=0.0,
                success=False,
                source=source,
            )
            await self._publish_workflow_step(
                incident_id=incident.incident_id,
                attempt=attempt,
                path_mode=path_mode,
                step="persist.class_memory",
                status="failure",
                class_id=class_match.class_id,
            )
        self._workflow_state = transition(self._workflow_state, WorkflowEvent.RECOVERY_FAILED)
        await self._events.publish(
            "decision.unusable",
            {
                "incident_id": incident.incident_id,
                "reason": reason,
                "candidate_actions": candidate_rows,
                "decision": decision_payload,
                "selected_action_key": selected_action_key,
                "attempts": incident.attempts,
            },
        )
        await self._events.publish(
            "incident.recovery_failed",
            {
                "incident_id": incident.incident_id,
                "tool_name": "decision_unusable",
                "error": reason,
                "attempts": incident.attempts,
            },
        )
        await self._publish_workflow_step(
            incident_id=incident.incident_id,
            attempt=attempt,
            path_mode=path_mode,
            step="attempt.result",
            status="failed",
            tool_name="decision_unusable",
            reason=reason,
        )

    async def _publish_fast_path_skipped(
        self,
        *,
        incident: IncidentContext,
        attempt: int,
        reason: str,
        decision_payload: dict[str, Any],
        candidate_rows: list[dict[str, Any]],
        class_match: ClassMatch | None,
        selected_action_key: str | None,
        fast_path_context: dict[str, Any] | None,
    ) -> None:
        payload: dict[str, Any] = {
            "incident_id": incident.incident_id,
            "reason": reason,
            "decision": decision_payload,
            "candidate_count": len(candidate_rows),
            "selected_action_key": selected_action_key,
            "fallback_source": "full_loop",
            "class_id": class_match.class_id if class_match is not None else None,
        }
        if fast_path_context is not None:
            payload["class_memory_candidate"] = fast_path_context
        await self._events.publish("class_memory.fast_path_skipped", payload)
        await self._publish_workflow_step(
            incident_id=incident.incident_id,
            attempt=attempt,
            path_mode="fast_path",
            step="path.fallback",
            status="fallback",
            reason=reason,
            fallback_path="full_loop",
        )

    @staticmethod
    def _selected_candidate_from_decision(
        *,
        candidate_rows: list[dict[str, Any]],
        decision_payload: dict[str, Any],
    ) -> DynamicToolSpec | None:
        selected_name = decision_payload.get("selected_candidate_name")
        selected_operation = decision_payload.get("selected_candidate_operation")
        normalized: list[DynamicToolSpec] = []
        for row in candidate_rows:
            try:
                normalized.append(DynamicToolSpec.model_validate(row))
            except Exception:  # noqa: S112 - malformed candidate rows are skipped by design
                continue

        if isinstance(selected_name, str) and selected_name:
            for item in normalized:
                if item.name == selected_name:
                    return item
        if isinstance(selected_operation, str) and selected_operation:
            for item in normalized:
                if item.operation == selected_operation:
                    return item
        return None

    _PLACEHOLDER_RE = re.compile(r"<[^>]+>")

    @classmethod
    def _is_placeholder(cls, value: Any) -> bool:
        if not isinstance(value, str):
            return False
        text = value.strip()
        return text == "" or bool(cls._PLACEHOLDER_RE.search(text))

    @classmethod
    def _normalize_candidate(cls, candidate: DynamicToolSpec) -> DynamicToolSpec | None:
        params = dict(candidate.params)
        operation = candidate.operation

        if operation == "run_sql":
            sql = params.get("sql")
            if not isinstance(sql, str) or sql.strip() == "":
                statements = params.get("statements")
                if isinstance(statements, list):
                    chunks = [str(item).strip() for item in statements if str(item).strip()]
                    if chunks:
                        sql = "\n".join(chunks)
            if not isinstance(sql, str) or sql.strip() == "" or cls._is_placeholder(sql):
                return None
            db_path = params.get("db_path")
            if not isinstance(db_path, str) or cls._is_placeholder(db_path):
                db_path = "data/app.db"
            return candidate.model_copy(
                update={
                    "params": {
                        "sql": sql.strip(),
                        "db_path": db_path.strip(),
                    }
                }
            )

        if operation == "replace_file":
            source = params.get("source_path")
            if not isinstance(source, str):
                source = params.get("source")
            destination = params.get("destination_path")
            if not isinstance(destination, str):
                destination = params.get("target_path")
            if not isinstance(source, str) or cls._is_placeholder(source):
                return None
            if not isinstance(destination, str) or cls._is_placeholder(destination):
                return None
            return candidate.model_copy(
                update={
                    "params": {
                        "source_path": source.strip(),
                        "destination_path": destination.strip(),
                    }
                }
            )

        if operation == "request_backup_restore":
            backup_id = params.get("backup_id")
            if not isinstance(backup_id, str) or backup_id.strip() == "":
                backup_id = params.get("selection") or params.get("restore_point")
            if not isinstance(backup_id, str) or cls._is_placeholder(backup_id):
                backup_id = "latest"
            normalized_backup = backup_id.strip() or "latest"
            return candidate.model_copy(update={"params": {"backup_id": normalized_backup}})

        if operation == "restart_service":
            service_name = params.get("service_name")
            if not isinstance(service_name, str) or cls._is_placeholder(service_name):
                service_name = "target-app"
            normalized_service = service_name.strip() or "target-app"
            return candidate.model_copy(
                update={"params": {"service_name": normalized_service}}
            )

        return candidate

    @classmethod
    def _normalize_how_proposal(cls, how: HowProposal) -> HowProposal:
        normalized: list[DynamicToolSpec] = []
        for candidate in [how.primary, *how.alternatives]:
            item = cls._normalize_candidate(candidate)
            if item is not None:
                normalized.append(item)
        if not normalized:
            return how
        return HowProposal(primary=normalized[0], alternatives=normalized[1:])

    @staticmethod
    def _decision_payload(decision: JevDecision) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "usable": decision.usable,
            "reason": decision.reason,
            "selected_candidate_name": decision.selected_candidate_name,
            "selected_candidate_operation": decision.selected_candidate_operation,
            "usable_probability": decision.usable_probability,
            "risk_score": decision.risk_score,
            "model_version": decision.model_version,
            "shadow": decision.shadow,
        }
        if (
            isinstance(decision.reason, str)
            and decision.reason.startswith("jev_request_error")
            and decision.raw
        ):
            payload["raw"] = decision.raw
        return payload

    def _build_symptom_query(self, diagnostics: dict[str, Any]) -> str:
        normalized_query = build_symptom_query(diagnostics, max_chars=None)
        return prepare_retrieval_query(
            query=normalized_query,
            prefix=self._rag_query_prefix,
            max_chars=self._rag_query_max_chars,
            rewrite_enabled=self._rag_query_rewrite,
        )

    def _build_repair_query(self, incident: IncidentContext) -> str:
        diagnosis = incident.diagnosis
        normalized_query = build_repair_query(
            diagnostics=incident.latest_diagnostics,
            failure_type=diagnosis.failure_type if diagnosis is not None else None,
            hypotheses=diagnosis.hypotheses if diagnosis is not None else [],
            last_tool_error=incident.last_tool_error,
            max_chars=None,
        )
        return prepare_retrieval_query(
            query=normalized_query,
            prefix=self._rag_query_prefix,
            max_chars=self._rag_query_max_chars,
            rewrite_enabled=self._rag_query_rewrite,
        )

    async def _retrieve_rag_chunks_for_incident(
        self,
        incident: IncidentContext,
        *,
        phase: str,
        query: str,
        attempt: int | None = None,
        path_mode: str = "full_loop",
    ) -> list[RAGChunk]:
        if not self._rag_query_rewrite:
            chunks = await self._tools.retrieve_rag_chunks(query, k=self._rag_top_k)
        else:
            raw_chunks = await self._tools.retrieve_rag_chunks(query, k=self._rag_fetch_depth)
            chunks = cap_chunks_per_source(raw_chunks, max_per_source=2, k=self._rag_top_k)

        meta: dict[str, Any] = {}
        consume_meta = getattr(self._tools, "consume_last_rag_meta", None)
        if callable(consume_meta):
            consumed = consume_meta()
            if isinstance(consumed, dict):
                meta = consumed

        status = str(meta.get("status") or ("ok" if chunks else "empty"))
        count_raw = meta.get("count")
        count_value: int
        if isinstance(count_raw, int):
            count_value = count_raw
        elif isinstance(count_raw, float) and count_raw.is_integer():
            count_value = int(count_raw)
        elif isinstance(count_raw, str):
            try:
                count_value = int(count_raw)
            except ValueError:
                count_value = len(chunks)
        else:
            count_value = len(chunks)
        status_payload: dict[str, Any] = {
            "status": status,
            "query_used": str(meta.get("query_used") or query)[:1200],
            "count": count_value,
        }
        if isinstance(meta.get("reason"), str):
            status_payload["reason"] = meta["reason"]
        if isinstance(meta.get("error_type"), str):
            status_payload["error_type"] = meta["error_type"]
        if isinstance(meta.get("error_message"), str):
            status_payload["error_message"] = meta["error_message"]

        if phase == "symptom":
            incident.rag_symptom_status = status_payload
        else:
            incident.rag_repair_status = status_payload

        incident.rag_call_counts[phase] = incident.rag_call_counts.get(phase, 0) + 1
        await self._events.publish(
            "rag.call",
            {
                "incident_id": incident.incident_id,
                "phase": phase,
                "call_count": incident.rag_call_counts[phase],
                "top_k": self._rag_top_k,
                "fetch_depth": self._rag_fetch_depth,
                "rewrite_enabled": self._rag_query_rewrite,
                **status_payload,
            },
        )

        if status == "skipped":
            await self._events.publish(
                "rag.skipped",
                {
                    "incident_id": incident.incident_id,
                    "phase": phase,
                    **status_payload,
                },
            )
        elif status == "empty":
            await self._events.publish(
                "rag.empty",
                {
                    "incident_id": incident.incident_id,
                    "phase": phase,
                    **status_payload,
                },
            )
        elif status == "error":
            await self._events.publish(
                "rag.error",
                {
                    "incident_id": incident.incident_id,
                    "phase": phase,
                    "error_type": status_payload.get("error_type", "RAGError"),
                    "error": status_payload.get("error_message", "unknown rag error"),
                    **status_payload,
                },
            )

        if attempt is not None:
            await self._publish_workflow_step(
                incident_id=incident.incident_id,
                attempt=attempt,
                path_mode=path_mode,
                step=f"rag.{phase}",
                status=status,
                query=status_payload.get("query_used"),
                count=status_payload.get("count"),
                reason=status_payload.get("reason"),
            )

        return chunks

    def _load_policy(self) -> dict[str, dict[str, float]]:
        path = Path(self._policy_store_path)
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(raw, dict):
            return {}
        actions_raw: dict[str, Any]
        if "actions" in raw and isinstance(raw.get("actions"), dict):
            actions_raw = raw["actions"]
        else:
            actions_raw = raw
        policy: dict[str, dict[str, float]] = {}
        for key, value in actions_raw.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            attempts = float(value.get("attempts", 0.0))
            reward_total = float(value.get("reward_total", 0.0))
            policy[key] = {"attempts": attempts, "reward_total": reward_total}
        return policy

    def _update_policy(self, action_key: str, *, reward: float) -> None:
        entry = self._policy.setdefault(action_key, {"attempts": 0.0, "reward_total": 0.0})
        entry["attempts"] += 1.0
        entry["reward_total"] += reward
        payload = {
            "updated_at": time.time(),
            "actions": self._policy,
        }
        path = Path(self._policy_store_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _append_skill_entry(
        self,
        *,
        incident: IncidentContext,
        tool_name: str,
        operation: str,
    ) -> None:
        path = Path(self._skills_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        if not path.exists():
            path.write_text(
                "# Recovery Skills Library\n\n"
                "Auto-learned skills from successful recovery sessions.\n",
                encoding="utf-8",
            )

        policy_entry = self._policy.get(tool_name, {})
        rl_attempts = float(policy_entry.get("attempts", 0.0))
        rl_reward_total = float(policy_entry.get("reward_total", 0.0))
        rl_avg_reward = rl_reward_total / rl_attempts if rl_attempts > 0 else 0.0

        diagnosis = incident.diagnosis
        failure_type = diagnosis.failure_type if diagnosis is not None else "unknown_failure"
        hypotheses = diagnosis.hypotheses if diagnosis is not None else []
        evidence = diagnosis.evidence if diagnosis is not None else []

        skill_name = f"recovery-{tool_name.strip().replace(' ', '-').lower() or 'dynamic-tool'}"
        version_patch = max(0, round(rl_attempts) - 1)
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        hypotheses_text = ", ".join(hypotheses[:3]) if hypotheses else "n/a"
        evidence_text = ", ".join(evidence[:3]) if evidence else "n/a"

        block = (
            "\n---\n"
            f"name: {skill_name}\n"
            "description: Learned successful recovery action "
            f"`{tool_name}` for `{failure_type}` incidents.\n"
            "triggers:\n"
            f"  - \"{failure_type}\"\n"
            f"  - \"recover {failure_type}\"\n"
            "tools:\n"
            f"  - {operation}\n"
            f"version: 1.0.{version_patch}\n"
            "---\n\n"
            f"# Skill: {tool_name}\n\n"
            "## Objective\n"
            f"Restore service health for `{failure_type}` incidents using `{operation}`.\n\n"
            "## Learned From Successful Session\n"
            f"- timestamp: {timestamp}\n"
            f"- incident_id: {incident.incident_id}\n"
            f"- attempts_to_success: {incident.attempts}\n"
            f"- failure_type: {failure_type}\n"
            f"- dominant_hypotheses: {hypotheses_text}\n"
            f"- key_evidence: {evidence_text}\n"
            f"- state_fingerprint: {incident.state_fingerprint or 'n/a'}\n\n"
            "## RL Knowledge\n"
            f"- action_key: {tool_name}\n"
            f"- rl_attempts: {rl_attempts:.0f}\n"
            f"- rl_reward_total: {rl_reward_total:.2f}\n"
            f"- rl_avg_reward: {rl_avg_reward:.2f}\n"
            "- cue: If this failure pattern repeats, prioritize this action "
            "before lower-confidence alternatives.\n"
            "- cue: Always verify with `/health/deep` after action execution.\n"
        )

        with path.open("a", encoding="utf-8") as handle:
            handle.write(block)

    def _load_skills_chunks(self) -> list[RAGChunk]:
        path = Path(self._skills_path)
        if not path.exists():
            return []
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if not content:
            return []

        tail = content[-120:]
        text = "\n".join(tail).strip()
        if text == "":
            return []

        return [
            RAGChunk(
                source=path.name,
                text=text,
                score=1.0,
                metadata={"type": "local_skills_memory"},
            )
        ]

    def _record_diagnosis_trace(
        self,
        *,
        incident_id: str,
        diagnosis: Any,
        rag_status: dict[str, Any],
    ) -> None:
        entry = self._trace_entry(incident_id)
        entry["why"] = sanitize_payload(diagnosis.model_dump(), max_string_length=1200)
        entry["rag"] = sanitize_payload(
            {
                "symptom": rag_status,
                "repair": entry.get("rag", {}).get("repair")
                if isinstance(entry.get("rag"), dict)
                else None,
            },
            max_string_length=1200,
        )
        raw = self._consume_llm_trace()
        if raw is not None:
            entry.setdefault("raw", {})["diagnose_why"] = raw
        entry["updated_ts"] = time.time()

    def _record_how_trace(
        self,
        *,
        incident_id: str,
        proposal: HowProposal,
        executed_tool_name: str,
        executed_candidate: str,
        jev_decision: dict[str, Any] | None,
        source: str,
        action_key: str,
        class_id: str | None,
        rag_symptom_status: dict[str, Any],
        rag_repair_status: dict[str, Any],
    ) -> None:
        entry = self._trace_entry(incident_id)
        how_payload: dict[str, Any] = {
            "primary": proposal.primary.model_dump(),
            "alternatives": [item.model_dump() for item in proposal.alternatives],
            "executed_tool_name": executed_tool_name,
            "executed_candidate": executed_candidate,
            "jev_decision": jev_decision,
        }
        if self._classmem_enabled:
            how_payload.update(
                {
                    "source": source,
                    "action_key": action_key,
                    "class_id": class_id,
                }
            )
        entry["how"] = sanitize_payload(
            how_payload,
            max_string_length=1200,
        )
        entry["rag"] = sanitize_payload(
            {
                "symptom": rag_symptom_status,
                "repair": rag_repair_status,
            },
            max_string_length=1200,
        )
        raw = self._consume_llm_trace()
        if raw is not None:
            entry.setdefault("raw", {})["propose_how"] = raw
        entry["updated_ts"] = time.time()

    def _trace_entry(self, incident_id: str) -> dict[str, Any]:
        if incident_id not in self._llm_traces:
            self._llm_traces[incident_id] = {
                "incident_id": incident_id,
                "updated_ts": time.time(),
                "why": None,
                "how": None,
                "rag": None,
                "raw": {},
            }
            self._llm_trace_order.append(incident_id)

        if len(self._llm_trace_order) > self._max_llm_trace_incidents:
            overflow = self._llm_trace_order[:-self._max_llm_trace_incidents]
            self._llm_trace_order = self._llm_trace_order[-self._max_llm_trace_incidents :]
            for stale_incident in overflow:
                self._llm_traces.pop(stale_incident, None)

        return self._llm_traces[incident_id]

    def _consume_llm_trace(self) -> dict[str, Any] | None:
        consume = getattr(self._llm, "consume_trace", None)
        if not callable(consume):
            return None
        payload = consume()
        if payload is None:
            return None
        safe_payload = sanitize_payload(payload, max_items=40, max_string_length=1200)
        if isinstance(safe_payload, dict):
            return safe_payload
        return {"value": safe_payload}
