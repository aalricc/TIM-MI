from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections import Counter, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from mi_agent.agent.diagnose import Diagnoser, probe_fingerprint
from mi_agent.agent.executor import AgentActionExecutor
from mi_agent.agent.monitor import MonitorLoop
from mi_agent.agent.reward import score_outcome
from mi_agent.agent.selector import EpsilonGreedySelector, SelectableAction
from mi_agent.agent.shield import ActionShield
from mi_agent.alerts.sink import AlertSink
from mi_agent.config import MISettings
from mi_agent.errors import (
    IncidentLockError,
    LLMOutputValidationError,
    LLMPermanentError,
    LLMUnavailableError,
    ToolError,
)
from mi_agent.events.bus import EventBus
from mi_agent.events.models import Event
from mi_agent.failure_taxonomy import FailureTaxonomy
from mi_agent.failure_types import UNCLASSIFIED_FAILURE, normalize_failure_type
from mi_agent.llm.client import LLMClient
from mi_agent.policy.decision_log import DecisionLogEntry, DecisionLogStore, RewardComponents
from mi_agent.policy.store import PolicyStore
from mi_agent.skills.distiller import SkillDistiller
from mi_agent.skills.library import SkillLibrary
from mi_agent.skills.matcher import SkillMatcher
from mi_agent.skills.model import SkillRecord
from mi_agent.supervisor.supervisor import IncidentLock, Supervisor
from mi_agent.tools.executor import ToolExecutor
from mi_agent.tools.schemas import CandidatePlan
from mi_agent.verifier.verifier import Verifier
from mi_agent.workflow.models import Diagnosis, EpisodeAction

_UNCLASSIFIED_FAILURE = UNCLASSIFIED_FAILURE
@dataclass(slots=True)
class MIContext:
    settings: MISettings
    events: EventBus
    supervisor: Supervisor
    verifier: Verifier
    tools: ToolExecutor
    policy: PolicyStore
    policy_version: str
    decision_log: DecisionLogStore
    failure_taxonomy: FailureTaxonomy
    skill_library: SkillLibrary
    llm: LLMClient
    selector: EpsilonGreedySelector
    skill_matcher: SkillMatcher
    skill_distiller: SkillDistiller
    alerts: AlertSink


@dataclass(frozen=True, slots=True)
class CandidateRejection:
    plan_id: str
    step_index: int
    action_key: str
    reason: str
    detail: str


@dataclass(frozen=True, slots=True)
class CandidateBuildResult:
    candidates: list[SelectableAction]
    rejections: list[CandidateRejection]
    needs_diagnosis: list[DiagnosePlan]


@dataclass(frozen=True, slots=True)
class PlanStepCandidate:
    plan_id: str
    step_index: int
    action_id: str
    parameters: dict[str, Any]
    action_key: str
    shield_state: dict[str, bool]
    risk_tier: str
    executable: bool


@dataclass(frozen=True, slots=True)
class DiagnosePlan:
    plan_id: str
    plan_class: str
    steps: list[PlanStepCandidate]


@dataclass(frozen=True, slots=True)
class PlannerTrace:
    planner_input_fields: tuple[str, ...]
    latency_ms: float
    token_estimate: int


class MIAgent:
    def __init__(self, context: MIContext) -> None:
        self._context = context
        self._diagnoser = Diagnoser(context.llm, context.tools, context.policy, context.settings)
        self._executor = AgentActionExecutor(
            context.tools,
            context.events,
            failed_action_ttl_seconds=context.settings.failed_action_ttl_seconds,
        )
        self._shield = ActionShield(context.settings, context.tools)
        self._incident_guard = asyncio.Lock()
        self._monitor = MonitorLoop(
            interval_seconds=context.settings.monitor_interval_seconds,
            ping_fn=self._ping_db,
            on_failure=self._handle_possible_failure,
            event_bus=context.events,
        )
        self._plan_cache: dict[tuple[str, str], list[CandidatePlan]] = {}

    async def start(self) -> None:
        await self._monitor.start()

    async def stop(self) -> None:
        await self._monitor.stop()

    async def _ping_db(self) -> bool:
        result = await self._context.tools.execute("ping_db", {})
        return bool(result.data.get("ok", False))

    async def _handle_possible_failure(self) -> None:
        if self._incident_guard.locked():
            return
        async with self._incident_guard:
            await self._recover_loop()

    async def _recover_loop(self) -> None:
        incident_failure_type = _UNCLASSIFIED_FAILURE

        try:
            incident = await self._context.supervisor.open_incident(incident_failure_type)
        except IncidentLockError:
            await self._context.events.publish(
                Event(
                    event_type="agent.incident_skipped_lock_busy",
                    payload={"failure_type": incident_failure_type},
                )
            )
            return

        llm_usage_start = self._snapshot_llm_usage()
        episode_actions: list[tuple[str, dict[str, Any], bool]] = []
        credited_actions: list[EpisodeAction] = []
        visits = 0
        resolved = False
        terminal_outcome: str | None = None
        state_fingerprint = ""
        permitted_primitives = self._context.tools.primitive_ids
        blocked_skill_names: set[str] = set()
        latest_planner_trace = PlannerTrace(
            planner_input_fields=(),
            latency_ms=0.0,
            token_estimate=0,
        )
        last_attempt = 0

        try:
            try:
                preflight = await self._context.llm.preflight()
                await self._context.events.publish(
                    Event(
                        event_type="llm.preflight_ok",
                        payload={
                            "incident_id": incident.incident_id,
                            "model": preflight.get("model"),
                            "endpoint_host": preflight.get("endpoint_host"),
                            "status": preflight.get("status"),
                            "summary": preflight.get("summary"),
                        },
                    )
                )
            except LLMPermanentError as exc:
                terminal_outcome = "llm_permanent_error"
                await self._context.events.publish(
                    Event(
                        event_type="llm.preflight_failed",
                        payload={"incident_id": incident.incident_id, "error": str(exc)},
                    )
                )

            while terminal_outcome is None and not resolved:
                gate = await self._context.supervisor.gate_attempt(incident)
                last_attempt = gate.attempts

                observe_started = perf_counter()
                recent_logs = await self._diagnoser.collect_logs()
                probes = await self._diagnoser.collect_probes()
                fingerprint = probe_fingerprint(probes, recent_logs)
                state_fingerprint = fingerprint["probes"]
                failed_keys = self._executor.failed_action_keys(state_fingerprint)
                has_recent_read_observation = True
                await self._emit_span(
                    "loop.observe",
                    observe_started,
                    attributes={"incident_id": incident.incident_id},
                )

                try:
                    matched_skill = self._match_skill_without_llm(
                        incident_failure_type,
                        fingerprint,
                    )
                    if matched_skill is None:
                        matched_skill = await self._safe_match_with_llm(
                            incident_failure_type,
                            fingerprint,
                        )
                    if matched_skill is not None:
                        skill_action_key = f"skill:{matched_skill.name}:v{matched_skill.version}"
                        if (
                            matched_skill.name in blocked_skill_names
                            or skill_action_key in failed_keys
                        ):
                            matched_skill = None

                    action_selected: SelectableAction | None = None
                    skill_used = False
                    candidate_action_keys: list[str] = []
                    propensities: dict[str, float] = {}

                    if matched_skill is not None:
                        action_key = f"skill:{matched_skill.name}:v{matched_skill.version}"
                        action_selected = SelectableAction(
                            action_key=action_key,
                            action_id=matched_skill.name,
                            parameters={},
                            value_score=self._context.policy.score(
                                incident_failure_type,
                                action_key,
                            ),
                        )
                        skill_used = True
                        candidate_action_keys = [action_key]
                        propensities = {action_key: 1.0}
                    else:
                        diagnosis_payload = await self._safe_diagnose(
                            incident_failure_type,
                            recent_logs,
                            probes,
                        )
                        if diagnosis_payload is None:
                            visits += 1
                            continue

                        diagnosis, _, _ = diagnosis_payload
                        inferred_failure_type = self._canonical_failure_type(diagnosis.failure_type)
                        if inferred_failure_type != _UNCLASSIFIED_FAILURE:
                            incident_failure_type = inferred_failure_type
                            incident.failure_type = incident_failure_type

                        matched_skill = self._match_skill_without_llm(
                            incident_failure_type,
                            fingerprint,
                        )
                        if matched_skill is None:
                            matched_skill = await self._safe_match_with_llm(
                                incident_failure_type,
                                fingerprint,
                            )
                        if matched_skill is not None:
                            skill_action_key = (
                                f"skill:{matched_skill.name}:v{matched_skill.version}"
                            )
                            if (
                                matched_skill.name in blocked_skill_names
                                or skill_action_key in failed_keys
                            ):
                                matched_skill = None
                        if matched_skill is not None:
                            action_key = f"skill:{matched_skill.name}:v{matched_skill.version}"
                            action_selected = SelectableAction(
                                action_key=action_key,
                                action_id=matched_skill.name,
                                parameters={},
                                value_score=self._context.policy.score(
                                    incident_failure_type,
                                    action_key,
                                ),
                            )
                            skill_used = True
                            candidate_action_keys = [action_key]
                            propensities = {action_key: 1.0}

                        if matched_skill is None:
                            plans = self._get_cached_plans(
                                failure_type=incident_failure_type,
                                state_fingerprint=state_fingerprint,
                            )
                            cache_hit = plans is not None
                            if plans is None:
                                plans, latest_planner_trace = await self._safe_propose_plans(
                                    diagnosis=diagnosis,
                                    incident_id=incident.incident_id,
                                    failure_type=incident_failure_type,
                                    permitted_primitives=permitted_primitives,
                                    failed_action_keys=failed_keys,
                                    constraints={
                                        "must_return_executable_action": False,
                                        "read_observations": {
                                            "recent_logs": recent_logs[-5:],
                                            "probes": probes,
                                        },
                                    },
                                )
                            else:
                                await self._context.events.publish(
                                    Event(
                                        event_type="agent.plan_cache_hit",
                                        payload={
                                            "incident_id": incident.incident_id,
                                            "failure_type": incident_failure_type,
                                        },
                                    )
                                )

                            build_result = await self._build_candidates_from_plans(
                                failure_type=incident_failure_type,
                                permitted_primitives=permitted_primitives,
                                plans=plans,
                                mutating_only=True,
                                attempt_number=gate.attempts,
                                has_recent_read_observation=has_recent_read_observation,
                            )
                            candidates = [
                                candidate
                                for candidate in build_result.candidates
                                if candidate.action_key not in failed_keys
                            ]
                            if not candidates and cache_hit:
                                kept_count = len(plans)
                                extra_plans, latest_planner_trace = await self._safe_propose_plans(
                                    diagnosis=diagnosis,
                                    incident_id=incident.incident_id,
                                    failure_type=incident_failure_type,
                                    permitted_primitives=permitted_primitives,
                                    failed_action_keys=failed_keys,
                                    constraints={
                                        "must_return_executable_action": False,
                                        "read_observations": {
                                            "recent_logs": recent_logs[-5:],
                                            "probes": probes,
                                        },
                                    },
                                )
                                plans = self._extend_cached_plans(
                                    failure_type=incident_failure_type,
                                    state_fingerprint=state_fingerprint,
                                    extra_plans=extra_plans,
                                )
                                await self._context.events.publish(
                                    Event(
                                        event_type="agent.plan_cache_extended",
                                        payload={
                                            "incident_id": incident.incident_id,
                                            "failure_type": incident_failure_type,
                                            "kept_plans": kept_count,
                                            "menu_plans": len(plans),
                                        },
                                    )
                                )
                                build_result = await self._build_candidates_from_plans(
                                    failure_type=incident_failure_type,
                                    permitted_primitives=permitted_primitives,
                                    plans=plans,
                                    mutating_only=True,
                                    attempt_number=gate.attempts,
                                    has_recent_read_observation=has_recent_read_observation,
                                )
                                candidates = [
                                    candidate
                                    for candidate in build_result.candidates
                                    if candidate.action_key not in failed_keys
                                ]

                            if not candidates:
                                await self._emit_no_viable_summary(
                                    incident_id=incident.incident_id,
                                    failure_type=incident_failure_type,
                                    state_fingerprint=state_fingerprint,
                                    attempt_number=gate.attempts,
                                    replan_attempt=0,
                                    cache_hit=cache_hit,
                                    plans=plans,
                                    rejections=build_result.rejections,
                                    planner_trace=latest_planner_trace,
                                )
                                visits += 1
                                continue

                            if not cache_hit:
                                self._store_cached_plans(
                                    failure_type=incident_failure_type,
                                    state_fingerprint=state_fingerprint,
                                    plans=plans,
                                )

                            candidate_action_keys = [
                                candidate.action_key for candidate in candidates
                            ]
                            propensities = self._context.selector.propensity_distribution(
                                candidates=candidates,
                                visits=visits,
                                has_proven_skill=bool(
                                    self._context.skill_library.all(incident_failure_type)
                                ),
                                failed_action_keys=failed_keys,
                                state_changed_since_failure=False,
                            )
                            action_selected, epsilon, mode = self._context.selector.select(
                                candidates=candidates,
                                visits=visits,
                                has_proven_skill=bool(
                                    self._context.skill_library.all(incident_failure_type)
                                ),
                                failed_action_keys=failed_keys,
                                state_changed_since_failure=False,
                            )
                            await self._context.events.publish(
                                Event(
                                    event_type="agent.selection",
                                    payload={
                                        "epsilon": epsilon,
                                        "mode": mode,
                                        "candidate_count": len(candidates),
                                        "selected_action": action_selected.action_key,
                                    },
                                )
                            )

                    if action_selected is None:
                        visits += 1
                        continue

                    if skill_used and matched_skill is not None:
                        preconditions_ok, precondition_errors = (
                            await self._recheck_skill_preconditions(
                                skill=matched_skill,
                                recent_logs=recent_logs,
                            )
                        )
                        if not preconditions_ok:
                            blocked_skill_names.add(matched_skill.name)
                            await self._context.events.publish(
                                Event(
                                    event_type="skills.precondition_failed",
                                    payload={
                                        "name": matched_skill.name,
                                        "reasons": precondition_errors,
                                    },
                                )
                            )
                            visits += 1
                            continue

                    if not skill_used:
                        precheck = self._shield.precheck_candidate(
                            action_id=action_selected.action_id,
                            parameters=action_selected.parameters,
                            attempt_number=gate.attempts,
                            has_recent_read_observation=has_recent_read_observation,
                        )
                        if not precheck.allowed:
                            visits += 1
                            continue

                        postcheck = self._shield.postcheck_candidate(
                            action_id=action_selected.action_id,
                            parameters=action_selected.parameters,
                            parameter_state=precheck.parameter_state,
                        )
                        if not postcheck.allowed:
                            visits += 1
                            continue

                    started = datetime.now(tz=UTC)
                    execute_started = perf_counter()
                    try:
                        if skill_used and matched_skill is not None:
                            execution = await self._executor.execute_skill(
                                skill=matched_skill,
                                allowed_primitives=permitted_primitives,
                                state_fingerprint=state_fingerprint,
                            )
                        else:
                            execution = await self._executor.execute_primitive(
                                action_id=action_selected.action_id,
                                parameters=action_selected.parameters,
                                allowed_primitives=permitted_primitives,
                                state_fingerprint=state_fingerprint,
                            )
                        await self._emit_span(
                            "loop.execute",
                            execute_started,
                            attributes={"incident_id": incident.incident_id, "status": "ok"},
                        )
                    except ToolError as exc:
                        await self._emit_span(
                            "loop.execute",
                            execute_started,
                            attributes={"incident_id": incident.incident_id, "status": "error"},
                        )
                        self._executor.mark_failed(action_selected.action_key, state_fingerprint)
                        await self._context.events.publish(
                            Event(
                                event_type="agent.action_failed",
                                payload={
                                    "failure_type": incident_failure_type,
                                    "error": str(exc),
                                    "skill_used": skill_used,
                                    "action": action_selected.action_key,
                                },
                            )
                        )
                        visits += 1
                        continue

                    verify_started = perf_counter()
                    verify_result = await self._context.verifier.verify(
                        failure_type=incident_failure_type
                    )
                    await self._emit_span(
                        "loop.verify",
                        verify_started,
                        attributes={"incident_id": incident.incident_id},
                    )
                    duration = (datetime.now(tz=UTC) - started).total_seconds()

                    risk_tier = self._execution_risk_tier(execution)
                    reward_components = score_outcome(
                        verifier_healthy=verify_result.healthy,
                        duration_seconds=duration,
                        attempts=gate.attempts,
                        risk_tier=risk_tier,
                        rolled_back=not verify_result.healthy,
                        used_last_resort=False,
                        data_loss_ratio=self._data_loss_ratio(verify_result.details),
                    )
                    self._observe_selector_reward(
                        action_key=execution.action_key,
                        reward=reward_components.net_reward,
                        skill_used=skill_used,
                    )
                    self._log_decision(
                        incident_id=incident.incident_id,
                        failure_type=incident_failure_type,
                        state_features={
                            "probe_fingerprint": state_fingerprint,
                            "attempt": str(gate.attempts),
                        },
                        candidate_set=candidate_action_keys or [execution.action_key],
                        chosen_action=execution.action_key,
                        propensities=propensities or {execution.action_key: 1.0},
                        verifier_healthy=verify_result.healthy,
                        reward_components=reward_components,
                        planner_trace=latest_planner_trace,
                        shield_outcome="allowed",
                        step_cost_ms=duration * 1000.0,
                        recovery_result="recovered" if verify_result.healthy else "not_recovered",
                    )

                    if verify_result.healthy:
                        if skill_used and matched_skill is not None:
                            for step in matched_skill.steps:
                                episode_actions.append((step.primitive_id, step.parameters, True))
                        else:
                            episode_actions.append(
                                (
                                    action_selected.action_id,
                                    action_selected.parameters,
                                    True,
                                )
                            )
                        credited_actions.append(
                            EpisodeAction(
                                action_key=execution.action_key,
                                reward=reward_components.net_reward,
                                duration_seconds=duration,
                                success=self._continuity_credit_ok(verify_result.details),
                            )
                        )
                        await self._context.events.publish(
                            Event(
                                event_type="agent.recovered",
                                payload={
                                    "incident": incident.incident_id,
                                    "failure_type": incident_failure_type,
                                    "attempts": gate.attempts,
                                    "duration_seconds": duration,
                                },
                            )
                        )
                        await self._on_resolved(
                            incident=incident,
                            failure_type=incident_failure_type,
                            fingerprint=fingerprint,
                            episode_actions=episode_actions,
                            credited_actions=credited_actions,
                        )
                        resolved = True
                        break

                    await self._executor.rollback_if_needed(execution.snapshot)
                    self._executor.mark_failed(execution.action_key, state_fingerprint)
                    visits += 1

                except LLMPermanentError as exc:
                    terminal_outcome = "llm_permanent_error"
                    await self._context.events.publish(
                        Event(
                            event_type="llm.permanent_error",
                            payload={"incident_id": incident.incident_id, "error": str(exc)},
                        )
                    )

            if not resolved:
                outcome = terminal_outcome or "unresolved_autonomous"
                await self._context.events.publish(
                    Event(
                        event_type=f"agent.{outcome}",
                        payload={
                            "incident": incident.incident_id,
                            "failure_type": incident_failure_type,
                        },
                    )
                )
                outcome_attempt = max(1, last_attempt)
                reward_components = score_outcome(
                    verifier_healthy=False,
                    duration_seconds=0.0,
                    attempts=outcome_attempt,
                    risk_tier="terminal",
                    rolled_back=False,
                    used_last_resort=False,
                    data_loss_ratio=0.0,
                )
                outcome_key = f"outcome:{outcome}"
                self._context.policy.update_action(
                    failure_type=incident_failure_type,
                    action_key=outcome_key,
                    reward=reward_components.net_reward,
                    success=False,
                )
                self._log_decision(
                    incident_id=incident.incident_id,
                    failure_type=incident_failure_type,
                    state_features={
                        "probe_fingerprint": state_fingerprint,
                        "attempt": str(outcome_attempt),
                    },
                    candidate_set=[outcome_key],
                    chosen_action=outcome_key,
                    propensities={outcome_key: 1.0},
                    verifier_healthy=False,
                    reward_components=reward_components,
                    planner_trace=latest_planner_trace,
                    shield_outcome="not_checked",
                    step_cost_ms=0.0,
                    recovery_result=outcome,
                )
        finally:
            llm_usage_end = self._snapshot_llm_usage()
            await self._context.events.publish(
                Event(
                    event_type="agent.llm_usage_incident",
                    payload={
                        "incident_id": incident.incident_id,
                        "llm_calls": max(0, llm_usage_end[0] - llm_usage_start[0]),
                        "llm_failures": max(0, llm_usage_end[1] - llm_usage_start[1]),
                        "estimated_cost_usd": max(0.0, llm_usage_end[2] - llm_usage_start[2]),
                    },
                )
            )
            await self._context.supervisor.release_incident(incident.incident_id)

    async def _on_resolved(
        self,
        *,
        incident: IncidentLock,
        failure_type: str,
        fingerprint: dict[str, str],
        episode_actions: list[tuple[str, dict[str, Any], bool]],
        credited_actions: list[EpisodeAction],
    ) -> None:
        failure_type = self._canonical_failure_type(failure_type)
        self._context.policy.update_episode(failure_type, credited_actions)
        if any(not success for _, _, success in episode_actions):
            lesson = await self._safe_write_lesson(
                failure_type,
                summary=f"Recovered incident {incident.incident_id} after failed attempts",
            )
            if lesson is not None:
                self._context.policy.record_lesson(failure_type, lesson)
            return

        if not episode_actions:
            return
        distill_input = [
            (action_id, params, success) for action_id, params, success in episode_actions
        ]
        distill_error: str | None = None
        try:
            skill = await self._context.skill_distiller.distill(
                failure_type=failure_type,
                episode_actions=distill_input,
                trigger_fingerprint=fingerprint,
                allowed_primitives=self._context.tools.primitive_ids,
            )
        except (LLMUnavailableError, LLMOutputValidationError, ToolError) as exc:
            distill_error = str(exc)
            skill = None

        if skill is not None:
            learned_skill = self._context.skill_library.register(
                skill,
                source_incident_id=incident.incident_id,
                environment_fingerprint=fingerprint,
            )
            await self._context.events.publish(
                Event(
                    event_type="skills.learned",
                    payload={"name": learned_skill.name, "version": learned_skill.version},
                )
            )
        elif distill_error is not None:
            await self._context.events.publish(
                Event(
                    event_type="skills.distill_failed",
                    payload={
                        "failure_type": failure_type,
                        "error": distill_error,
                    },
                )
            )

    def _match_skill_without_llm(
        self,
        failure_type: str,
        fingerprint: dict[str, str],
    ) -> Any | None:
        if failure_type == _UNCLASSIFIED_FAILURE:
            candidates = self._context.skill_library.all()
        else:
            candidates = self._context.skill_library.all(failure_type)
        for skill in candidates:
            if skill.trigger_fingerprint == fingerprint:
                return skill
        return None

    async def _recheck_skill_preconditions(
        self,
        *,
        skill: SkillRecord,
        recent_logs: list[str],
    ) -> tuple[bool, list[str]]:
        try:
            probes = await self._diagnoser.collect_probes()
        except ToolError as exc:
            return False, [f"probe recheck failed: {exc}"]

        fingerprint = probe_fingerprint(probes, recent_logs)
        failures: list[str] = []
        for precondition in skill.preconditions:
            holds, reason = self._evaluate_skill_precondition(
                precondition,
                probes=probes,
                fingerprint=fingerprint,
            )
            if not holds and reason:
                failures.append(reason)
        return len(failures) == 0, failures

    @classmethod
    def _evaluate_skill_precondition(
        cls,
        precondition: str,
        *,
        probes: dict[str, Any],
        fingerprint: dict[str, str],
    ) -> tuple[bool, str]:
        condition = precondition.strip()
        if condition == "":
            return True, ""
        if condition.startswith("probe:"):
            return cls._evaluate_mapping_precondition(
                expression=condition.removeprefix("probe:"),
                mapping=probes,
                source="probe",
            )
        if condition.startswith("fingerprint:"):
            return cls._evaluate_mapping_precondition(
                expression=condition.removeprefix("fingerprint:"),
                mapping=fingerprint,
                source="fingerprint",
            )
        return True, ""

    @classmethod
    def _evaluate_mapping_precondition(
        cls,
        *,
        expression: str,
        mapping: dict[str, Any],
        source: str,
    ) -> tuple[bool, str]:
        if "=" not in expression:
            return False, f"Malformed {source} precondition: {expression}"

        raw_path, raw_expected = expression.split("=", 1)
        path = raw_path.strip()
        if path == "":
            return False, f"Malformed {source} precondition: {expression}"

        found, actual = cls._lookup_nested_mapping_value(mapping, path)
        if not found:
            return False, f"{source} precondition path not found: {path}"

        expected = cls._parse_precondition_literal(raw_expected)
        if actual != expected:
            return False, (
                f"{source} precondition failed: {path} expected {expected!r} got {actual!r}"
            )
        return True, ""

    @staticmethod
    def _lookup_nested_mapping_value(mapping: dict[str, Any], path: str) -> tuple[bool, Any]:
        current: Any = mapping
        for segment in path.split("."):
            if not isinstance(current, dict) or segment not in current:
                return False, None
            current = current[segment]
        return True, current

    @staticmethod
    def _parse_precondition_literal(raw_value: str) -> Any:
        token = raw_value.strip()
        lowered = token.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        if lowered in {"null", "none"}:
            return None
        if (token.startswith('"') and token.endswith('"')) or (
            token.startswith("'") and token.endswith("'")
        ):
            return token[1:-1]
        try:
            return int(token)
        except ValueError:
            pass
        try:
            return float(token)
        except ValueError:
            return token

    async def _safe_match_with_llm(
        self,
        failure_type: str,
        fingerprint: dict[str, str],
    ) -> Any | None:
        span_started = perf_counter()
        if failure_type == _UNCLASSIFIED_FAILURE:
            await self._emit_span("loop.match_skill", span_started)
            return None
        try:
            matched = await self._context.skill_matcher.match(
                failure_type=failure_type,
                trigger_fingerprint=fingerprint,
            )
            await self._emit_span("loop.match_skill", span_started)
            return matched
        except LLMPermanentError:
            await self._emit_span("loop.match_skill", span_started, attributes={"status": "error"})
            raise
        except (LLMUnavailableError, LLMOutputValidationError) as exc:
            await self._emit_span("loop.match_skill", span_started, attributes={"status": "error"})
            await self._context.events.publish(
                Event(
                    event_type="llm.match_unavailable",
                    payload={"failure_type": failure_type, "error": str(exc)},
                )
            )
            return None

    async def _safe_diagnose(
        self,
        failure_type: str,
        recent_logs: list[str],
        probes: dict[str, Any],
    ) -> tuple[Diagnosis, dict[str, Any], dict[str, str]] | None:
        span_started = perf_counter()
        hint = None if failure_type == _UNCLASSIFIED_FAILURE else failure_type
        attempt = 0
        while True:
            try:
                diagnosis = await self._diagnoser.diagnose(
                    failure_type_hint=hint,
                    recent_logs=recent_logs,
                    probes=probes,
                )
                await self._emit_span("loop.diagnose", span_started)
                return diagnosis
            except LLMPermanentError:
                await self._emit_span("loop.diagnose", span_started, attributes={"status": "error"})
                raise
            except (LLMUnavailableError, LLMOutputValidationError) as exc:
                attempt += 1
                delay = self._retry_delay(attempt)
                await self._context.events.publish(
                    Event(
                        event_type="llm.diagnose_retry",
                        payload={
                            "failure_type": failure_type,
                            "attempt": attempt,
                            "retry_in_seconds": round(delay, 3),
                            "error": str(exc),
                        },
                    )
                )
                await asyncio.sleep(delay)

    async def _safe_propose_plans(
        self,
        *,
        diagnosis: Diagnosis,
        incident_id: str,
        failure_type: str,
        permitted_primitives: frozenset[str],
        failed_action_keys: set[str],
        constraints: dict[str, Any] | None = None,
    ) -> tuple[list[CandidatePlan], PlannerTrace]:
        span_started = perf_counter()
        planner_fields = (
            "diagnosis",
            "available_actions",
            "action_parameter_contracts",
            "recalled_skills",
            "failed_action_keys",
            "constraints",
        )
        usage_before = self._snapshot_llm_usage()
        attempt = 0
        while True:
            try:
                plans = await self._context.llm.propose_plans(
                    diagnosis=diagnosis,
                    available_actions=sorted(permitted_primitives),
                    action_parameter_contracts=self._context.tools.primitive_parameter_contracts(),
                    recalled_skills=[
                        skill.name for skill in self._context.skill_library.all(failure_type)
                    ],
                    failed_action_keys=sorted(failed_action_keys),
                    constraints=constraints,
                )
                await self._emit_span("loop.plan", span_started)
                usage_after = self._snapshot_llm_usage()
                trace = PlannerTrace(
                    planner_input_fields=planner_fields,
                    latency_ms=max(0.0, (perf_counter() - span_started) * 1000.0),
                    token_estimate=self._estimate_token_delta(usage_before, usage_after),
                )
                await self._context.events.publish(
                    Event(
                        event_type="agent.planner_call",
                        payload={
                            "incident_id": incident_id,
                            "failure_type": failure_type,
                            "attempt": attempt + 1,
                            "input_fields": list(planner_fields),
                            "constraints_fields": sorted((constraints or {}).keys()),
                            "latency_ms": trace.latency_ms,
                            "token_estimate": trace.token_estimate,
                        },
                    )
                )
                return plans, trace
            except LLMPermanentError:
                await self._emit_span("loop.plan", span_started, attributes={"status": "error"})
                raise
            except (LLMUnavailableError, LLMOutputValidationError) as exc:
                attempt += 1
                delay = self._retry_delay(attempt)
                await self._context.events.publish(
                    Event(
                        event_type="llm.plan_retry",
                        payload={
                            "failure_type": failure_type,
                            "attempt": attempt,
                            "retry_in_seconds": round(delay, 3),
                            "error": str(exc),
                        },
                    )
                )
                await asyncio.sleep(delay)

    def _retry_delay(self, attempt: int) -> float:
        bounded_attempt = min(max(0, attempt - 1), 8)
        base = min(self._context.settings.llm_retry_max_delay_seconds, 2**bounded_attempt)
        jitter_span = base * self._context.settings.llm_retry_jitter_ratio
        jitter = float(random.uniform(-jitter_span, jitter_span))
        return float(max(0.05, base + jitter))

    def _plan_cache_key(self, failure_type: str, state_fingerprint: str) -> tuple[str, str]:
        return (self._canonical_failure_type(failure_type), state_fingerprint)

    def _get_cached_plans(
        self,
        *,
        failure_type: str,
        state_fingerprint: str,
    ) -> list[CandidatePlan] | None:
        key = self._plan_cache_key(failure_type, state_fingerprint)
        cached = self._plan_cache.get(key)
        if cached is None:
            return None
        return [plan.model_copy(deep=True) for plan in cached]

    def _store_cached_plans(
        self,
        *,
        failure_type: str,
        state_fingerprint: str,
        plans: list[CandidatePlan],
    ) -> None:
        if not plans:
            return
        key = self._plan_cache_key(failure_type, state_fingerprint)
        self._plan_cache[key] = [plan.model_copy(deep=True) for plan in plans]

    def _extend_cached_plans(
        self,
        *,
        failure_type: str,
        state_fingerprint: str,
        extra_plans: list[CandidatePlan],
    ) -> list[CandidatePlan]:
        existing = self._get_cached_plans(
            failure_type=failure_type,
            state_fingerprint=state_fingerprint,
        ) or []
        seen = {self._plan_menu_signature(plan) for plan in existing}
        merged = list(existing)
        for plan in extra_plans:
            signature = self._plan_menu_signature(plan)
            if signature in seen:
                continue
            seen.add(signature)
            merged.append(plan.model_copy(deep=True))
        self._store_cached_plans(
            failure_type=failure_type,
            state_fingerprint=state_fingerprint,
            plans=merged,
        )
        return merged

    @staticmethod
    def _plan_menu_signature(plan: CandidatePlan) -> tuple[str, tuple[tuple[str, str], ...]]:
        step = plan.steps[0]
        parameters = tuple(sorted((key, repr(value)) for key, value in step.parameters.items()))
        return (step.action_id, parameters)

    async def _safe_write_lesson(self, failure_type: str, summary: str) -> str | None:
        try:
            return await self._context.llm.write_lesson(failure_type=failure_type, summary=summary)
        except (LLMUnavailableError, LLMOutputValidationError):
            return None

    async def _build_candidates_from_plans(
        self,
        *,
        failure_type: str,
        permitted_primitives: frozenset[str],
        plans: list[Any],
        mutating_only: bool,
        attempt_number: int,
        has_recent_read_observation: bool,
    ) -> CandidateBuildResult:
        candidates: list[SelectableAction] = []
        rejections: list[CandidateRejection] = []
        needs_diagnosis: list[DiagnosePlan] = []
        for plan in plans:
            plan_id = str(getattr(plan, "name", "")).strip() or "unnamed-plan"
            if not plan.steps:
                rejections.append(
                    CandidateRejection(
                        plan_id=plan_id,
                        step_index=-1,
                        action_key="",
                        reason="plan_empty",
                        detail="Plan has no steps",
                    )
                )
                continue
            validated_steps: list[PlanStepCandidate] = []
            plan_valid = True
            for index, step in enumerate(plan.steps):
                if step.action_id not in permitted_primitives:
                    rejections.append(
                        CandidateRejection(
                            plan_id=plan_id,
                            step_index=index,
                            action_key="",
                            reason="step_not_permitted",
                            detail=f"Step primitive not permitted: {step.action_id}",
                        )
                    )
                    plan_valid = False
                    break

                merged_parameters = self._context.tools.with_default_parameters(
                    step.action_id,
                    step.parameters,
                )
                try:
                    validated = self._context.tools.validate_action_schema(
                        step.action_id,
                        merged_parameters,
                    )
                except ToolError as exc:
                    error_detail = self._sanitize_rejection_detail(str(exc))
                    await self._context.events.publish(
                        Event(
                            event_type="llm.plan_invalid_step",
                            payload={
                                "failure_type": failure_type,
                                "action_id": step.action_id,
                                "step_index": index,
                                "error": error_detail,
                            },
                        )
                    )
                    rejections.append(
                        CandidateRejection(
                            plan_id=plan_id,
                            step_index=index,
                            action_key="",
                            reason="invalid_parameters",
                            detail=error_detail,
                        )
                    )
                    plan_valid = False
                    break

                parameters = validated.model_dump()
                step_risk_tier = self._context.tools.risk_tier(step.action_id)
                shield_precheck = self._shield.precheck_candidate(
                    action_id=step.action_id,
                    parameters=parameters,
                    attempt_number=attempt_number,
                    has_recent_read_observation=has_recent_read_observation,
                )
                if not shield_precheck.allowed:
                    reason = shield_precheck.reason or "unknown"
                    reason_key = f"shield_pre_blocked:{reason}"
                    await self._context.events.publish(
                        Event(
                            event_type="agent.shield_pre_blocked",
                            payload={
                                "failure_type": failure_type,
                                "action_id": step.action_id,
                                "step_index": index,
                                "risk_tier": step_risk_tier,
                                "reason": reason,
                            },
                        )
                    )
                    rejections.append(
                        CandidateRejection(
                            plan_id=plan_id,
                            step_index=index,
                            action_key="",
                            reason=reason_key,
                            detail=(
                                "Shield pre-check blocked step "
                                f"{step.action_id} with reason {reason}"
                            ),
                        )
                    )
                    plan_valid = False
                    break

                validated_steps.append(
                    PlanStepCandidate(
                        plan_id=plan_id,
                        step_index=index,
                        action_id=step.action_id,
                        parameters=parameters,
                        action_key=self._executor.action_key(step.action_id, parameters),
                        shield_state=shield_precheck.parameter_state,
                        risk_tier=step_risk_tier,
                        executable=self._is_executable_action(step.action_id, step_risk_tier),
                    )
                )

            if not plan_valid or not validated_steps:
                if plan_valid:
                    rejections.append(
                        CandidateRejection(
                            plan_id=plan_id,
                            step_index=0,
                            action_key="",
                            reason="missing_first_step_state",
                            detail="First step parameters or shield state missing",
                        )
                    )
                continue

            first_step = validated_steps[0]
            if mutating_only and not first_step.executable:
                rejections.append(
                    CandidateRejection(
                        plan_id=plan_id,
                        step_index=0,
                        action_key=first_step.action_key,
                        reason="first_step_not_executable",
                        detail=(
                            "First step must be mutating, brokered, terminal, or wait "
                            f"when mutating_only is enabled (got {first_step.action_id})"
                        ),
                    )
                )
                continue

            candidates.append(
                SelectableAction(
                    action_key=first_step.action_key,
                    action_id=first_step.action_id,
                    parameters=first_step.parameters,
                    value_score=self._context.policy.score(failure_type, first_step.action_key),
                    shield_state=first_step.shield_state,
                )
            )
        return CandidateBuildResult(
            candidates=candidates,
            rejections=rejections,
            needs_diagnosis=needs_diagnosis,
        )

    @staticmethod
    def _is_executable_action(action_id: str, risk_tier: str) -> bool:
        if action_id == "wait":
            return True
        if risk_tier in {"brokered", "terminal"}:
            return True
        return risk_tier != "read_only"

    def _planner_constraints_payload(
        self,
        *,
        state_fingerprint: str,
        recent_rejections: deque[CandidateRejection],
        readonly_round: int,
        max_rounds: int,
        force_executable: bool,
        attempt_number: int,
        incident_elapsed_seconds: float,
        read_observations: list[dict[str, Any]] | None = None,
        state_snapshot: dict[str, Any] | None = None,
        state_version: str | None = None,
    ) -> dict[str, Any]:
        state_hash = hashlib.sha256(state_fingerprint.encode("utf-8")).hexdigest()[:16]
        constraints: dict[str, Any] = {
            "state_snapshot_hash": state_hash,
            "state_version": state_version or state_hash,
            "recent_rejections": self._recent_rejection_feedback(recent_rejections),
            "readonly_round": readonly_round,
            "readonly_round_cap": max_rounds,
            "must_return_executable_action": force_executable,
            "attempt_context": {
                "attempt_number": attempt_number,
                "incident_elapsed_seconds": round(max(0.0, incident_elapsed_seconds), 3),
            },
            "objective": {
                "autonomous_mode": True,
                "human_in_loop": False,
                "primary_goal": "restore_verifier_health_fast",
                "secondary_goal": "preserve_data_minimize_irreversible_risk",
            },
        }
        if read_observations:
            constraints["read_observations"] = read_observations
        if state_snapshot is not None:
            constraints["state_snapshot"] = state_snapshot
        return constraints

    def _recent_rejection_feedback(
        self,
        recent_rejections: deque[CandidateRejection],
    ) -> list[dict[str, Any]]:
        limit = self._context.settings.readonly_replan_recent_rejection_limit
        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str, int, str]] = set()
        for rejection in reversed(list(recent_rejections)):
            short_detail = self._sanitize_rejection_detail(rejection.detail)
            payload = {
                "reason": rejection.reason,
                "action_key": rejection.action_key,
                "step_index": rejection.step_index,
                "short_detail": short_detail,
            }
            dedupe_key = (
                rejection.reason,
                rejection.action_key,
                rejection.step_index,
                short_detail,
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            deduped.append(payload)
            if len(deduped) >= limit:
                break
        deduped.reverse()
        return deduped

    async def _emit_no_viable_summary(
        self,
        *,
        incident_id: str,
        failure_type: str,
        state_fingerprint: str,
        attempt_number: int,
        replan_attempt: int,
        cache_hit: bool,
        plans: list[Any],
        rejections: list[CandidateRejection],
        planner_trace: PlannerTrace | None = None,
    ) -> None:
        reason_counts = Counter(rejection.reason for rejection in rejections)
        sample_limit = self._context.settings.candidate_rejection_summary_sample_limit
        samples = [
            {
                "plan_id": rejection.plan_id,
                "step_index": rejection.step_index,
                "action_key": rejection.action_key,
                "reason": rejection.reason,
                "short_detail": self._sanitize_rejection_detail(rejection.detail),
            }
            for rejection in rejections[:sample_limit]
        ]
        if self._context.settings.candidate_rejection_summary_enabled:
            await self._context.events.publish(
                Event(
                    event_type="agent.candidates_rejected_summary",
                    payload={
                        "incident_id": incident_id,
                        "failure_type": failure_type,
                        "state_fingerprint": state_fingerprint,
                        "attempt_number": attempt_number,
                        "replan_attempt": replan_attempt,
                        "cache_status": "hit" if cache_hit else "miss",
                        "plans_received": len(plans),
                        "reason_counts": dict(sorted(reason_counts.items())),
                        "rejected_samples": samples,
                    },
                )
            )

        await self._context.events.publish(
            Event(
                event_type="agent.no_viable_candidates",
                payload={"failure_type": failure_type},
            )
        )

        if self._context.settings.candidate_rejection_log_decision_enabled:
            summary_json = json.dumps(dict(sorted(reason_counts.items())), sort_keys=True)
            sampled_keys = [
                rejection.action_key
                for rejection in rejections
                if rejection.action_key.strip() != ""
            ]
            candidate_set = sampled_keys or ["agent.no_viable_candidates"]
            reward_components = score_outcome(
                verifier_healthy=False,
                duration_seconds=0.0,
                attempts=attempt_number,
                risk_tier="read_only",
                rolled_back=False,
                used_last_resort=False,
                data_loss_ratio=0.0,
            )
            self._log_decision(
                incident_id=incident_id,
                failure_type=failure_type,
                state_features={
                    "probe_fingerprint": state_fingerprint,
                    "attempt": str(attempt_number),
                    "candidate_rejection_counts": summary_json,
                    "candidate_replan_attempt": str(replan_attempt),
                    "candidate_cache_status": "hit" if cache_hit else "miss",
                },
                candidate_set=candidate_set,
                chosen_action="agent.no_viable_candidates",
                propensities={"agent.no_viable_candidates": 1.0},
                verifier_healthy=False,
                reward_components=reward_components,
                planner_trace=planner_trace,
                shield_outcome="n/a",
                recovery_result="rejected",
            )

    @staticmethod
    def _sanitize_rejection_detail(detail: str) -> str:
        compact = " ".join(detail.strip().split())
        return compact[:200]

    @staticmethod
    def _normalize_failure_type(raw: str) -> str:
        return normalize_failure_type(raw)

    def _canonical_failure_type(self, raw: str) -> str:
        normalized = normalize_failure_type(raw)
        return self._context.failure_taxonomy.resolve(normalized)

    def _log_decision(
        self,
        *,
        incident_id: str,
        failure_type: str,
        state_features: dict[str, str],
        candidate_set: list[str],
        chosen_action: str,
        propensities: dict[str, float],
        verifier_healthy: bool | None,
        reward_components: RewardComponents,
        planner_trace: PlannerTrace | None = None,
        shield_outcome: str = "not_checked",
        step_cost_ms: float | None = None,
        recovery_result: str | None = None,
    ) -> None:
        features = dict(state_features)
        probe_fingerprint = features.get("probe_fingerprint", "")
        features.setdefault(
            "state_snapshot_hash",
            hashlib.sha256(probe_fingerprint.encode("utf-8")).hexdigest()[:16],
        )
        if planner_trace is not None:
            features["planner_input_fields"] = json.dumps(
                list(planner_trace.planner_input_fields),
                sort_keys=True,
            )
            features["planner_latency_ms"] = f"{planner_trace.latency_ms:.3f}"
            features["planner_token_estimate"] = str(planner_trace.token_estimate)
        else:
            features.setdefault("planner_input_fields", "[]")
            features.setdefault("planner_latency_ms", "0.000")
            features.setdefault("planner_token_estimate", "0")

        features["shield_outcome"] = shield_outcome
        features["step_cost_ms"] = f"{max(0.0, step_cost_ms or 0.0):.3f}"
        if recovery_result is None:
            if verifier_healthy is True:
                recovery_result = "recovered"
            elif chosen_action == "agent.no_viable_candidates":
                recovery_result = "rejected"
            else:
                recovery_result = "pending" if verifier_healthy is None else "not_recovered"
        features["recovery_result"] = recovery_result

        entry = DecisionLogEntry(
            incident_id=incident_id,
            failure_type=self._canonical_failure_type(failure_type),
            state_features=features,
            candidate_set=candidate_set,
            chosen_action=chosen_action,
            propensities=propensities,
            policy_version=self._context.policy_version,
            verifier_healthy=verifier_healthy,
            reward_components=reward_components,
        )
        self._context.decision_log.append(entry)

    def _execution_risk_tier(self, execution: Any) -> str:
        if not execution.step_results:
            return "reversible"
        ordered = {
            "read_only": 0,
            "reversible": 1,
            "brokered": 2,
            "irreversible": 3,
            "terminal": 4,
        }
        tiers = [
            self._context.tools.risk_tier(result.primitive_id)
            for result in execution.step_results
        ]
        return max(tiers, key=lambda tier: ordered.get(tier, 1))

    @staticmethod
    def _continuity_credit_ok(details: dict[str, Any]) -> bool:
        continuity = details.get("data_continuity")
        if not isinstance(continuity, dict):
            return False
        checked = continuity.get("checked")
        missing = continuity.get("missing")
        if not isinstance(checked, int) or checked <= 0:
            return False
        if not isinstance(missing, int) or missing < 0:
            return False
        return missing == 0

    @staticmethod
    def _data_loss_ratio(details: dict[str, Any]) -> float:
        continuity = details.get("data_continuity")
        if not isinstance(continuity, dict):
            return 0.0
        checked = continuity.get("checked")
        missing = continuity.get("missing")
        if not isinstance(checked, int) or checked <= 0:
            return 0.0
        if not isinstance(missing, int) or missing < 0:
            return 0.0
        return min(1.0, missing / checked)

    def _observe_selector_reward(self, *, action_key: str, reward: float, skill_used: bool) -> None:
        if skill_used:
            return
        self._context.selector.observe(action_key=action_key, reward=reward)

    def _snapshot_llm_usage(self) -> tuple[int, int, float]:
        llm_client = self._context.llm
        usage = getattr(llm_client, "usage", None)
        if usage is not None:
            calls = int(getattr(usage, "calls", 0))
            failures = int(getattr(usage, "failures", 0))
            cost = float(getattr(usage, "estimated_cost_usd", 0.0))
            return calls, failures, cost

        call_map = getattr(llm_client, "calls", None)
        failure_map = getattr(llm_client, "failures", None)
        calls = sum(call_map.values()) if isinstance(call_map, dict) else 0
        failures = sum(failure_map.values()) if isinstance(failure_map, dict) else 0
        return int(calls), int(failures), 0.0

    @staticmethod
    def _estimate_token_delta(
        before: tuple[int, int, float],
        after: tuple[int, int, float],
    ) -> int:
        cost_delta = max(0.0, after[2] - before[2])
        if cost_delta <= 0:
            return 0
        return round(cost_delta / 0.000001)

    async def _emit_span(
        self,
        name: str,
        started_at: float,
        *,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        duration_ms = max(0.0, (perf_counter() - started_at) * 1000.0)
        await self._context.events.publish(
            Event(
                event_type="otel.span",
                payload={
                    "schema_version": "v1",
                    "name": name,
                    "duration_ms": duration_ms,
                    "attributes": attributes or {},
                },
            )
        )
