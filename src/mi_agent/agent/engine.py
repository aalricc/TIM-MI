from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mi_agent.decision.client import JevDecision, JevDecisionClient
from mi_agent.events import EventLog
from mi_agent.llm.client import LLMClient
from mi_agent.observability import sanitize_payload
from mi_agent.tools.executor import ToolExecutor
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
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._verifier = verifier
        self._events = events
        self._policy_store_path = policy_store_path
        self._monitor_interval_seconds = monitor_interval_seconds
        self._jev = jev
        default_skills_path = str(Path(policy_store_path).with_name("skills.md"))
        self._skills_path = skills_path or default_skills_path

        self._workflow_state = WorkflowState.HEALTHY
        self._incident: IncidentContext | None = None
        self._last_monitor: MonitorStatus | None = None
        self._policy: dict[str, dict[str, float]] = self._load_policy()
        self._llm_traces: dict[str, dict[str, Any]] = {}
        self._llm_trace_order: list[str] = []
        self._max_llm_trace_incidents = 120

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
        self._workflow_state = transition(self._workflow_state, WorkflowEvent.UNHEALTHY_DETECTED)

        await self._attempt_recovery(self._incident, retry_from_step5=False)

    async def _attempt_recovery(
        self,
        incident: IncidentContext,
        *,
        retry_from_step5: bool,
    ) -> None:
        if retry_from_step5 and incident.diagnosis is not None:
            diagnosis = incident.diagnosis
        else:
            diagnostics = await self._tools.collect_diagnostics()
            symptom_query = self._build_symptom_query(diagnostics)
            incident.rag_symptom_chunks = await self._tools.retrieve_rag_chunks(
                symptom_query,
                k=5,
            )

            diagnosis = await self._llm.diagnose_why(
                evidence=diagnostics,
                rag_chunks=incident.rag_symptom_chunks,
            )
            incident.diagnosis = diagnosis
            self._record_diagnosis_trace(incident_id=incident.incident_id, diagnosis=diagnosis)
            await self._events.publish(
                "diagnosis.why",
                {
                    "incident_id": incident.incident_id,
                    **diagnosis.model_dump(),
                },
            )

        repair_query = self._build_repair_query(incident)
        incident.rag_repair_chunks = await self._tools.retrieve_rag_chunks(
            repair_query,
            k=5,
        )
        incident.rag_repair_chunks.extend(self._load_skills_chunks())

        how = await self._llm.propose_how(
            diagnosis=diagnosis,
            rag_chunks=incident.rag_repair_chunks,
            prior_attempts=incident.prior_attempts,
            latest_tool_error=incident.last_tool_error,
            state_fingerprint=incident.state_fingerprint,
        )

        selected_how, executed_candidate, jev_decision = await self._select_candidate_with_jev(
            incident=incident,
            diagnosis=diagnosis,
            how=how,
        )
        if selected_how is None:
            return

        tool_name = self._tools.register_temporary_dynamic_tool(incident.incident_id, selected_how)
        self._record_how_trace(
            incident_id=incident.incident_id,
            proposal=how,
            executed_tool_name=tool_name,
            executed_candidate=executed_candidate,
            jev_decision=jev_decision,
        )
        await self._events.publish(
            "planning.how",
            {
                "incident_id": incident.incident_id,
                "executed_tool_name": tool_name,
                "primary": how.primary.model_dump(),
                "alternatives": [item.model_dump() for item in how.alternatives],
                "executed_candidate": executed_candidate,
                "jev_decision": jev_decision,
            },
        )

        action_result = await self._tools.execute_dynamic_tool(incident.incident_id, tool_name)
        await self._events.publish(
            "dynamic_tool.executed",
            {"incident_id": incident.incident_id, "tool_name": tool_name, **action_result},
        )

        verification = await self._verifier.verify()
        await self._events.publish(
            "verification.result",
            {"incident_id": incident.incident_id, **verification.model_dump()},
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

        if verification.healthy:
            self._tools.persist_dynamic_tool(incident.incident_id, tool_name)
            self._tools.discard_incident_tools(incident.incident_id, keep=tool_name)
            self._update_policy(tool_name, reward=1.0)
            self._append_skill_entry(
                incident=incident,
                tool_name=tool_name,
                operation=selected_how.operation,
            )
            self._workflow_state = transition(
                self._workflow_state,
                WorkflowEvent.RECOVERY_SUCCEEDED,
            )
            await self._events.publish(
                "incident.recovered",
                {
                    "incident_id": incident.incident_id,
                    "tool_name": tool_name,
                    "attempts": incident.attempts,
                },
            )
            return

        incident.last_tool_error = str(
            action_result.get("error") or verification.failure_type or "unknown"
        )
        self._tools.discard_incident_tools(incident.incident_id)
        self._update_policy(tool_name, reward=-1.0)
        self._workflow_state = transition(self._workflow_state, WorkflowEvent.RECOVERY_FAILED)
        await self._events.publish(
            "incident.recovery_failed",
            {
                "incident_id": incident.incident_id,
                "tool_name": tool_name,
                "error": incident.last_tool_error,
                "attempts": incident.attempts,
            },
        )

    async def _select_candidate_with_jev(
        self,
        *,
        incident: IncidentContext,
        diagnosis: Any,
        how: HowProposal,
    ) -> tuple[DynamicToolSpec | None, str, dict[str, Any] | None]:
        candidates = [how.primary, *how.alternatives]
        candidate_rows = [candidate.model_dump() for candidate in candidates]

        if self._jev is None:
            await self._handle_unusable_decision(
                incident=incident,
                reason="jev_not_configured",
                candidate_rows=candidate_rows,
                decision_payload={"usable": False, "reason": "jev_not_configured"},
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
        decision = await self._jev.choose_candidate(state=state, candidates=candidate_rows)
        decision_payload = self._decision_payload(decision)
        if not decision.usable:
            await self._handle_unusable_decision(
                incident=incident,
                reason=decision.reason or "jev_decision_unusable",
                candidate_rows=candidate_rows,
                decision_payload=decision_payload,
            )
            return None, "none", decision_payload

        selected = self._pick_candidate(candidates, decision)
        if selected is None:
            await self._handle_unusable_decision(
                incident=incident,
                reason="selected_candidate_not_found",
                candidate_rows=candidate_rows,
                decision_payload=decision_payload,
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
    ) -> None:
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
                },
            )
        )
        self._update_policy("decision_unusable", reward=-1.0)
        self._workflow_state = transition(self._workflow_state, WorkflowEvent.RECOVERY_FAILED)
        await self._events.publish(
            "decision.unusable",
            {
                "incident_id": incident.incident_id,
                "reason": reason,
                "candidate_actions": candidate_rows,
                "decision": decision_payload,
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

    @staticmethod
    def _decision_payload(decision: JevDecision) -> dict[str, Any]:
        return {
            "usable": decision.usable,
            "reason": decision.reason,
            "selected_candidate_name": decision.selected_candidate_name,
            "selected_candidate_operation": decision.selected_candidate_operation,
            "usable_probability": decision.usable_probability,
            "risk_score": decision.risk_score,
            "model_version": decision.model_version,
        }

    @staticmethod
    def _build_symptom_query(diagnostics: dict[str, Any]) -> str:
        parts: list[str] = ["sqlite recovery", "symptom grounding"]
        health = diagnostics.get("health_deep")
        if isinstance(health, dict):
            parts.append(str(health.get("error") or "unknown_health_error"))
        integrity = diagnostics.get("db_integrity")
        if isinstance(integrity, dict):
            rows = integrity.get("rows")
            if isinstance(rows, list) and rows:
                parts.append(str(rows[0]))
        log_tail = diagnostics.get("log_tail")
        if isinstance(log_tail, list) and log_tail:
            parts.append(str(log_tail[-1])[:500])
        return " | ".join(parts)

    @staticmethod
    def _build_repair_query(incident: IncidentContext) -> str:
        pieces = ["sqlite repair guidance", "dynamic recovery"]
        if incident.diagnosis is not None:
            pieces.append(incident.diagnosis.failure_type)
            pieces.extend(incident.diagnosis.hypotheses[:2])
        if incident.last_tool_error:
            pieces.append(f"latest_tool_error={incident.last_tool_error}")
        return " | ".join(pieces)

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

    def _record_diagnosis_trace(self, *, incident_id: str, diagnosis: Any) -> None:
        entry = self._trace_entry(incident_id)
        entry["why"] = sanitize_payload(diagnosis.model_dump(), max_string_length=1200)
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
    ) -> None:
        entry = self._trace_entry(incident_id)
        entry["how"] = sanitize_payload(
            {
                "primary": proposal.primary.model_dump(),
                "alternatives": [item.model_dump() for item in proposal.alternatives],
                "executed_tool_name": executed_tool_name,
                "executed_candidate": executed_candidate,
                "jev_decision": jev_decision,
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
