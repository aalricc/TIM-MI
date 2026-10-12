from __future__ import annotations

import asyncio
import hashlib
import json
import re
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
from mi_agent.supervisor.last_resort import LastResortError
from mi_agent.supervisor.supervisor import IncidentLock, Supervisor
from mi_agent.tools.executor import ToolExecutor
from mi_agent.tools.schemas import CandidatePlan
from mi_agent.verifier.verifier import Verifier
from mi_agent.workflow.models import Diagnosis, EpisodeAction

_UNCLASSIFIED_FAILURE = UNCLASSIFIED_FAILURE
_READONLY_REPLAN_ALLOWED_APP_FILES = frozenset(
    {
        "schema.sql",
        "app.log",
        "config/runtime.env",
        "config/app.env",
    }
)
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9-]{20,}"),
    re.compile(
        r"(?i)\b[A-Za-z0-9_]*(api[_-]?key|token|secret|password)\b\s*[:=]\s*[^\s,;\\]+"
    ),
)


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


@dataclass(frozen=True, slots=True)
class ReadonlyReplanOutcome:
    plans: list[Any]
    build_result: CandidateBuildResult
    replan_attempt: int
    cache_hit: bool
    escalation_action: SelectableAction | None
    planner_trace: PlannerTrace


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
        permitted_primitives = self._context.tools.primitive_ids
        blocked_skill_names: set[str] = set()
        recent_rejections: deque[CandidateRejection] = deque(
            maxlen=self._context.settings.readonly_replan_recent_rejection_limit
        )
        latest_planner_trace = PlannerTrace(
            planner_input_fields=(),
            latency_ms=0.0,
            token_estimate=0,
        )

        try:
            while True:
                gate = await self._context.supervisor.gate_attempt(incident)
                if gate.cap_reached:
                    try:
                        await self._on_cap_reached(
                            incident,
                            incident_failure_type,
                            credited_actions,
                        )
                    except LastResortError as exc:
                        await self._context.events.publish(
                            Event(
                                event_type="supervisor.last_resort_failed",
                                payload={
                                    "incident_id": incident.incident_id,
                                    "error": str(exc),
                                },
                            )
                        )
                    break

                observe_started = perf_counter()
                recent_logs = await self._diagnoser.collect_logs()
                probes = await self._diagnoser.collect_probes()
                has_recent_read_observation = True
                fingerprint = probe_fingerprint(probes, recent_logs)
                state_fingerprint = fingerprint["probes"]
                await self._emit_span(
                    "loop.observe",
                    observe_started,
                    attributes={"incident_id": incident.incident_id},
                )

                matched_skill = self._match_skill_without_llm(incident_failure_type, fingerprint)
                if matched_skill is None:
                    matched_skill = await self._safe_match_with_llm(
                        incident_failure_type,
                        fingerprint,
                    )
                if matched_skill is not None and matched_skill.name in blocked_skill_names:
                    matched_skill = None
                if matched_skill is not None and incident_failure_type == _UNCLASSIFIED_FAILURE:
                    incident_failure_type = self._canonical_failure_type(matched_skill.failure_type)
                    incident.failure_type = incident_failure_type

                action_selected: SelectableAction | None = None
                skill_used = False
                candidate_action_keys: list[str] = []
                propensities: dict[str, float] = {}

                if matched_skill is not None:
                    action_key = f"skill:{matched_skill.name}:v{matched_skill.version}"
                    score = self._context.policy.score(incident_failure_type, action_key)
                    action_selected = SelectableAction(
                        action_key=action_key,
                        action_id=matched_skill.name,
                        parameters={},
                        value_score=score,
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
                        degraded_skill = self._trusted_skill_for_degraded_mode(
                            incident_failure_type,
                            fingerprint,
                        )
                        if (
                            degraded_skill is not None
                            and degraded_skill.name in blocked_skill_names
                        ):
                            degraded_skill = None
                        if degraded_skill is not None:
                            action_key = f"skill:{degraded_skill.name}:v{degraded_skill.version}"
                            score = self._context.policy.score(incident_failure_type, action_key)
                            action_selected = SelectableAction(
                                action_key=action_key,
                                action_id=degraded_skill.name,
                                parameters={},
                                value_score=score,
                            )
                            matched_skill = degraded_skill
                            skill_used = True
                            candidate_action_keys = [action_key]
                            propensities = {action_key: 1.0}
                            await self._context.events.publish(
                                Event(
                                    event_type="agent.degraded_mode",
                                    payload={
                                        "incident_id": incident.incident_id,
                                        "mode": "trusted_skill",
                                        "skill": degraded_skill.name,
                                    },
                                )
                            )
                        else:
                            action_selected = self._build_escalation_action(
                                failure_type=incident_failure_type,
                                incident_id=incident.incident_id,
                                state_fingerprint=state_fingerprint,
                                recent_logs=recent_logs,
                                actions_taken=[
                                    action.action_key
                                    for action in credited_actions[-5:]
                                ],
                            )
                            candidate_action_keys = [action_selected.action_key]
                            propensities = {action_selected.action_key: 1.0}
                            await self._context.events.publish(
                                Event(
                                    event_type="agent.degraded_mode",
                                    payload={
                                        "incident_id": incident.incident_id,
                                        "mode": "escalate",
                                        "failure_type": incident_failure_type,
                                    },
                                )
                            )
                        if action_selected is None:
                            visits += 1
                            continue
                    else:
                        diagnosis, _, _ = diagnosis_payload
                        inferred_failure_type = self._canonical_failure_type(
                            diagnosis.failure_type
                        )
                        if (
                            inferred_failure_type != _UNCLASSIFIED_FAILURE
                            and inferred_failure_type != incident_failure_type
                        ):
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
                        if matched_skill is not None and matched_skill.name in blocked_skill_names:
                            matched_skill = None
                        if matched_skill is not None:
                            action_key = f"skill:{matched_skill.name}:v{matched_skill.version}"
                            score = self._context.policy.score(incident_failure_type, action_key)
                            action_selected = SelectableAction(
                                action_key=action_key,
                                action_id=matched_skill.name,
                                parameters={},
                                value_score=score,
                            )
                            skill_used = True
                            candidate_action_keys = [action_key]
                            propensities = {action_key: 1.0}
                        else:
                            failed_keys = {
                                action.action_key
                                for action in credited_actions
                                if not action.success
                            }
                            planner_constraints = self._planner_constraints_payload(
                                state_fingerprint=state_fingerprint,
                                recent_rejections=recent_rejections,
                                readonly_round=0,
                                max_rounds=self._context.settings.max_readonly_rounds_per_incident,
                                force_executable=False,
                                state_snapshot=probes,
                                state_version=hashlib.sha256(
                                    state_fingerprint.encode("utf-8")
                                ).hexdigest()[:16],
                            )

                            plans = self._get_cached_plans(
                                failure_type=incident_failure_type,
                                state_fingerprint=state_fingerprint,
                            )
                            plans_from_cache = plans is not None
                            if plans is None:
                                plans, latest_planner_trace = await self._safe_propose_plans(
                                    diagnosis=diagnosis,
                                    incident_id=incident.incident_id,
                                    failure_type=incident_failure_type,
                                    permitted_primitives=permitted_primitives,
                                    failed_action_keys=failed_keys,
                                    constraints=planner_constraints,
                                )
                                self._store_cached_plans(
                                    failure_type=incident_failure_type,
                                    state_fingerprint=state_fingerprint,
                                    plans=plans,
                                )
                            else:
                                latest_planner_trace = PlannerTrace(
                                    planner_input_fields=("cache_hit",),
                                    latency_ms=0.0,
                                    token_estimate=0,
                                )
                                await self._context.events.publish(
                                    Event(
                                        event_type="agent.plan_cache_hit",
                                        payload={
                                            "failure_type": incident_failure_type,
                                            "state_fingerprint": state_fingerprint,
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
                            recent_rejections.extend(build_result.rejections)
                            candidates = build_result.candidates
                            if not candidates:
                                if (
                                    self._context.settings.readonly_replan_enabled
                                    and build_result.needs_diagnosis
                                ):
                                    readonly_outcome = await self._run_readonly_replan_rounds(
                                        incident_id=incident.incident_id,
                                        failure_type=incident_failure_type,
                                        state_fingerprint=state_fingerprint,
                                        diagnosis=diagnosis,
                                        permitted_primitives=permitted_primitives,
                                        failed_action_keys=failed_keys,
                                        attempt_number=gate.attempts,
                                        initial_build=build_result,
                                        recent_rejections=recent_rejections,
                                    )
                                    plans = readonly_outcome.plans
                                    build_result = readonly_outcome.build_result
                                    candidates = build_result.candidates
                                    latest_planner_trace = readonly_outcome.planner_trace
                                    recent_rejections.extend(build_result.rejections)
                                    if readonly_outcome.escalation_action is not None:
                                        action_selected = readonly_outcome.escalation_action
                                        candidates = [action_selected]
                                        candidate_action_keys = [action_selected.action_key]
                                        propensities = {action_selected.action_key: 1.0}
                                    if not candidates:
                                        await self._emit_no_viable_summary(
                                            incident_id=incident.incident_id,
                                            failure_type=incident_failure_type,
                                            state_fingerprint=state_fingerprint,
                                            attempt_number=gate.attempts,
                                            replan_attempt=readonly_outcome.replan_attempt,
                                            cache_hit=readonly_outcome.cache_hit,
                                            plans=plans,
                                            rejections=build_result.rejections,
                                            planner_trace=latest_planner_trace,
                                        )
                                        visits += 1
                                        continue
                                else:
                                    await self._emit_no_viable_summary(
                                        incident_id=incident.incident_id,
                                        failure_type=incident_failure_type,
                                        state_fingerprint=state_fingerprint,
                                        attempt_number=gate.attempts,
                                        replan_attempt=0,
                                        cache_hit=plans_from_cache,
                                        plans=plans,
                                        rejections=build_result.rejections,
                                        planner_trace=latest_planner_trace,
                                    )
                                    visits += 1
                                    continue

                            if failed_keys:
                                filtered = [
                                    candidate
                                    for candidate in candidates
                                    if candidate.action_key not in failed_keys
                                ]
                                filtered_out = [
                                    candidate
                                    for candidate in candidates
                                    if candidate.action_key in failed_keys
                                ]
                                filtered_rejections = [
                                    CandidateRejection(
                                        plan_id="candidate-filter",
                                        step_index=0,
                                        action_key=candidate.action_key,
                                        reason="failed_action_key",
                                        detail=(
                                            "Candidate removed because action key failed in"
                                            " unchanged state"
                                        ),
                                    )
                                    for candidate in filtered_out
                                ]
                                candidates = filtered
                                if filtered_rejections:
                                    build_result = CandidateBuildResult(
                                        candidates=build_result.candidates,
                                        rejections=[*build_result.rejections, *filtered_rejections],
                                        needs_diagnosis=build_result.needs_diagnosis,
                                    )
                                    recent_rejections.extend(filtered_rejections)

                            replan_attempt = 0
                            final_cache_hit = plans_from_cache
                            if not candidates and plans_from_cache and failed_keys:
                                replan_attempt = 1
                                plans, latest_planner_trace = await self._safe_propose_plans(
                                    diagnosis=diagnosis,
                                    incident_id=incident.incident_id,
                                    failure_type=incident_failure_type,
                                    permitted_primitives=permitted_primitives,
                                    failed_action_keys=failed_keys,
                                    constraints=planner_constraints,
                                )
                                self._store_cached_plans(
                                    failure_type=incident_failure_type,
                                    state_fingerprint=state_fingerprint,
                                    plans=plans,
                                )
                                build_result = await self._build_candidates_from_plans(
                                    failure_type=incident_failure_type,
                                    permitted_primitives=permitted_primitives,
                                    plans=plans,
                                    mutating_only=True,
                                    attempt_number=gate.attempts,
                                    has_recent_read_observation=has_recent_read_observation,
                                )
                                candidates = build_result.candidates
                                if failed_keys:
                                    filtered = [
                                        candidate
                                        for candidate in candidates
                                        if candidate.action_key not in failed_keys
                                    ]
                                    filtered_out = [
                                        candidate
                                        for candidate in candidates
                                        if candidate.action_key in failed_keys
                                    ]
                                    filtered_rejections = [
                                        CandidateRejection(
                                            plan_id="candidate-filter",
                                            step_index=0,
                                            action_key=candidate.action_key,
                                            reason="failed_action_key",
                                            detail=(
                                                "Candidate removed because action key failed in"
                                                " unchanged state"
                                            ),
                                        )
                                        for candidate in filtered_out
                                    ]
                                    candidates = filtered
                                    if filtered_rejections:
                                        build_result = CandidateBuildResult(
                                            candidates=build_result.candidates,
                                            rejections=[
                                                *build_result.rejections,
                                                *filtered_rejections,
                                            ],
                                            needs_diagnosis=build_result.needs_diagnosis,
                                        )
                                        recent_rejections.extend(filtered_rejections)
                                final_cache_hit = False
                                recent_rejections.extend(build_result.rejections)

                            if not candidates:
                                await self._emit_no_viable_summary(
                                    incident_id=incident.incident_id,
                                    failure_type=incident_failure_type,
                                    state_fingerprint=state_fingerprint,
                                    attempt_number=gate.attempts,
                                    replan_attempt=replan_attempt,
                                    cache_hit=final_cache_hit,
                                    plans=plans,
                                    rejections=build_result.rejections,
                                    planner_trace=latest_planner_trace,
                                )
                                visits += 1
                                continue

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
                                        "action": action_selected.action_key,
                                    },
                                )
                            )

                if skill_used and matched_skill is not None:
                    preconditions_ok, precondition_failures = (
                        await self._recheck_skill_preconditions(
                            skill=matched_skill,
                            recent_logs=recent_logs,
                        )
                    )
                    if not preconditions_ok:
                        blocked_skill_names.add(matched_skill.name)
                        await self._context.events.publish(
                            Event(
                                event_type="agent.skill_precondition_failed",
                                payload={
                                    "incident_id": incident.incident_id,
                                    "skill": matched_skill.name,
                                    "reasons": precondition_failures,
                                },
                            )
                        )
                        skill_used = False
                        matched_skill = None
                        action_selected = None
                        candidate_action_keys = []
                        propensities = {}
                        visits += 1
                        continue

                if (
                    action_selected is not None
                    and not skill_used
                    and action_selected.shield_state is not None
                ):
                    shield_postcheck = self._shield.postcheck_candidate(
                        action_id=action_selected.action_id,
                        parameters=action_selected.parameters,
                        parameter_state=action_selected.shield_state,
                    )
                    if not shield_postcheck.allowed:
                        blocked_reason = shield_postcheck.reason or "unknown"
                        await self._context.events.publish(
                            Event(
                                event_type="agent.shield_post_blocked",
                                payload={
                                    "failure_type": incident_failure_type,
                                    "action": action_selected.action_key,
                                    "reason": blocked_reason,
                                },
                            )
                        )
                        recent_rejections.append(
                            CandidateRejection(
                                plan_id="runtime-postcheck",
                                step_index=0,
                                action_key=action_selected.action_key,
                                reason=f"shield_post_blocked:{blocked_reason}",
                                detail="Candidate blocked by post-check before execution",
                            )
                        )
                        self._executor.mark_failed(action_selected.action_key, state_fingerprint)
                        risk_tier = self._context.tools.risk_tier(action_selected.action_id)
                        reward_components = score_outcome(
                            verifier_healthy=False,
                            duration_seconds=0.0,
                            attempts=gate.attempts,
                            risk_tier=risk_tier,
                            rolled_back=False,
                            used_last_resort=False,
                            data_loss_ratio=0.0,
                        )
                        reward = reward_components.net_reward
                        credited_actions.append(
                            EpisodeAction(
                                action_key=action_selected.action_key,
                                reward=reward,
                                duration_seconds=0.0,
                                success=False,
                            )
                        )
                        self._observe_selector_reward(
                            action_key=action_selected.action_key,
                            reward=reward,
                            skill_used=skill_used,
                        )
                        self._log_decision(
                            incident_id=incident.incident_id,
                            failure_type=incident_failure_type,
                            state_features={
                                "probe_fingerprint": state_fingerprint,
                                "attempt": str(gate.attempts),
                            },
                            candidate_set=candidate_action_keys or [action_selected.action_key],
                            chosen_action=action_selected.action_key,
                            propensities=propensities or {action_selected.action_key: 1.0},
                            verifier_healthy=None,
                            reward_components=reward_components,
                            planner_trace=latest_planner_trace,
                            shield_outcome="post_blocked",
                            step_cost_ms=0.0,
                        )
                        visits += 1
                        continue

                started = datetime.now(tz=UTC)
                execute_started = perf_counter()
                try:
                    if skill_used and matched_skill is not None and action_selected is not None:
                        execution = await self._executor.execute_skill(
                            skill=matched_skill,
                            allowed_primitives=permitted_primitives,
                            state_fingerprint=state_fingerprint,
                        )
                    elif action_selected is not None:
                        execution = await self._executor.execute_primitive(
                            action_id=action_selected.action_id,
                            parameters=action_selected.parameters,
                            allowed_primitives=permitted_primitives,
                            state_fingerprint=state_fingerprint,
                        )
                    else:
                        visits += 1
                        continue
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
                    await self._context.events.publish(
                        Event(
                            event_type="agent.action_failed",
                            payload={
                                "failure_type": incident_failure_type,
                                "error": str(exc),
                                "skill_used": skill_used,
                                "action": (
                                    action_selected.action_key
                                    if action_selected is not None
                                    else None
                                ),
                            },
                        )
                    )
                    if action_selected is not None:
                        self._executor.mark_failed(action_selected.action_key, state_fingerprint)
                        episode_actions.append(
                            (action_selected.action_id, action_selected.parameters, False)
                        )
                        risk_tier = self._context.tools.risk_tier(action_selected.action_id)
                        reward_components = score_outcome(
                            verifier_healthy=False,
                            duration_seconds=0.0,
                            attempts=gate.attempts,
                            risk_tier=risk_tier,
                            rolled_back=False,
                            used_last_resort=False,
                            data_loss_ratio=0.0,
                        )
                        reward = reward_components.net_reward
                        credited_actions.append(
                            EpisodeAction(
                                action_key=action_selected.action_key,
                                reward=reward,
                                duration_seconds=0.0,
                                success=False,
                            )
                        )
                        self._observe_selector_reward(
                            action_key=action_selected.action_key,
                            reward=reward,
                            skill_used=skill_used,
                        )
                        self._log_decision(
                            incident_id=incident.incident_id,
                            failure_type=incident_failure_type,
                            state_features={
                                "probe_fingerprint": state_fingerprint,
                                "attempt": str(gate.attempts),
                            },
                            candidate_set=candidate_action_keys or [action_selected.action_key],
                            chosen_action=action_selected.action_key,
                            propensities=propensities or {action_selected.action_key: 1.0},
                            verifier_healthy=None,
                            reward_components=reward_components,
                            planner_trace=latest_planner_trace,
                            shield_outcome="execution_error",
                            step_cost_ms=0.0,
                        )
                    visits += 1
                    continue

                terminal_steps = [
                    step_result.primitive_id
                    for step_result in execution.step_results
                    if self._context.tools.risk_tier(step_result.primitive_id) == "terminal"
                ]
                if terminal_steps:
                    if skill_used and matched_skill is not None:
                        for step in matched_skill.steps:
                            episode_actions.append((step.primitive_id, step.parameters, False))
                    elif action_selected is not None:
                        episode_actions.append(
                            (action_selected.action_id, action_selected.parameters, False)
                        )
                    credited_actions.append(
                        EpisodeAction(
                            action_key=execution.action_key,
                            reward=0.0,
                            duration_seconds=0.0,
                            success=False,
                            metadata={"terminal": ",".join(terminal_steps)},
                        )
                    )
                    reward_components = score_outcome(
                        verifier_healthy=False,
                        duration_seconds=0.0,
                        attempts=gate.attempts,
                        risk_tier="terminal",
                        rolled_back=False,
                        used_last_resort=False,
                        data_loss_ratio=0.0,
                    )
                    credited_actions[-1].reward = reward_components.net_reward
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
                        verifier_healthy=None,
                        reward_components=reward_components,
                        planner_trace=latest_planner_trace,
                        shield_outcome="allowed",
                        step_cost_ms=0.0,
                        recovery_result="escalated",
                    )
                    await self._context.events.publish(
                        Event(
                            event_type="agent.escalated_to_operator",
                            payload={
                                "incident_id": incident.incident_id,
                                "failure_type": incident_failure_type,
                                "action_key": execution.action_key,
                                "steps": terminal_steps,
                            },
                        )
                    )
                    break

                verify_started = perf_counter()
                verify_result = await self._context.verifier.verify(
                    failure_type=incident_failure_type,
                )
                await self._emit_span(
                    "loop.verify",
                    verify_started,
                    attributes={"incident_id": incident.incident_id},
                )
                ended = datetime.now(tz=UTC)
                duration = (ended - started).total_seconds()

                if verify_result.healthy:
                    risk_tier = self._execution_risk_tier(execution)
                    data_loss_ratio = self._data_loss_ratio(verify_result.details)
                    reward_components = score_outcome(
                        verifier_healthy=True,
                        duration_seconds=duration,
                        attempts=gate.attempts,
                        risk_tier=risk_tier,
                        rolled_back=False,
                        used_last_resort=False,
                        data_loss_ratio=data_loss_ratio,
                    )
                    reward = reward_components.net_reward
                    if skill_used and matched_skill is not None:
                        for step in matched_skill.steps:
                            episode_actions.append((step.primitive_id, step.parameters, True))
                    elif action_selected is not None:
                        episode_actions.append(
                            (action_selected.action_id, action_selected.parameters, True)
                        )
                    credited_actions.append(
                        EpisodeAction(
                            action_key=execution.action_key,
                            reward=reward,
                            duration_seconds=duration,
                            success=True,
                        )
                    )
                    self._observe_selector_reward(
                        action_key=execution.action_key,
                        reward=reward,
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
                        verifier_healthy=True,
                        reward_components=reward_components,
                        planner_trace=latest_planner_trace,
                        shield_outcome="allowed",
                        step_cost_ms=duration * 1000.0,
                        recovery_result="recovered",
                    )
                    if skill_used and matched_skill is not None:
                        self._context.skill_library.record_result(
                            matched_skill.name,
                            success=True,
                            recovery_seconds=duration,
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
                risk_tier = self._execution_risk_tier(execution)
                data_loss_ratio = self._data_loss_ratio(verify_result.details)
                reward_components = score_outcome(
                    verifier_healthy=False,
                    duration_seconds=duration,
                    attempts=gate.attempts,
                    risk_tier=risk_tier,
                    rolled_back=True,
                    used_last_resort=False,
                    data_loss_ratio=data_loss_ratio,
                )
                reward = reward_components.net_reward
                if skill_used and matched_skill is not None:
                    for step in matched_skill.steps:
                        episode_actions.append((step.primitive_id, step.parameters, False))
                elif action_selected is not None:
                    episode_actions.append(
                        (action_selected.action_id, action_selected.parameters, False)
                    )
                credited_actions.append(
                    EpisodeAction(
                        action_key=execution.action_key,
                        reward=reward,
                        duration_seconds=duration,
                        success=False,
                    )
                )
                self._observe_selector_reward(
                    action_key=execution.action_key,
                    reward=reward,
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
                    verifier_healthy=False,
                    reward_components=reward_components,
                    planner_trace=latest_planner_trace,
                    shield_outcome="allowed",
                    step_cost_ms=duration * 1000.0,
                    recovery_result="not_recovered",
                )
                if skill_used and matched_skill is not None:
                    self._context.skill_library.record_result(
                        matched_skill.name,
                        success=False,
                        recovery_seconds=duration,
                    )
                    self._context.skill_library.retire_if_repeated_failures(matched_skill.name)
                visits += 1

            if not resolved:
                await self._context.events.publish(
                    Event(
                        event_type="agent.incident_unresolved",
                        payload={"incident": incident.incident_id},
                    )
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

    async def _on_cap_reached(
        self,
        incident: IncidentLock,
        failure_type: str,
        credited_actions: list[EpisodeAction],
    ) -> None:
        failure_type = self._canonical_failure_type(failure_type)
        backup_path = await self._context.supervisor.run_last_resort(incident)
        verify = await self._context.verifier.verify(failure_type=failure_type)
        await self._context.alerts.send(
            title="MI cap reached",
            message=(
                f"incident={incident.incident_id} backup={backup_path} healthy={verify.healthy}"
            ),
        )
        for action in credited_actions:
            action.reward -= 2.0
            action.success = False
        if credited_actions:
            self._context.policy.update_episode(failure_type, credited_actions)
        lesson = await self._safe_write_lesson(
            failure_type,
            summary=(
                f"Cap reached for incident {incident.incident_id}; last-resort restore triggered"
            ),
        )
        if lesson is not None:
            self._context.policy.record_lesson(failure_type, lesson)

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

    def _trusted_skill_for_degraded_mode(
        self,
        failure_type: str,
        fingerprint: dict[str, str],
    ) -> Any | None:
        if failure_type == _UNCLASSIFIED_FAILURE:
            candidates = self._context.skill_library.all()
        else:
            candidates = self._context.skill_library.all(failure_type)
        trusted = [
            skill
            for skill in candidates
            if skill.active and skill.stats.success_count > 0
        ]
        if not trusted:
            return None

        for skill in trusted:
            if skill.trigger_fingerprint == fingerprint:
                return skill

        return max(
            trusted,
            key=lambda item: (
                item.stats.success_count,
                -item.stats.failure_count,
                item.version,
            ),
        )

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

    def _build_escalation_action(
        self,
        *,
        failure_type: str,
        incident_id: str,
        state_fingerprint: str,
        recent_logs: list[str],
        actions_taken: list[str],
    ) -> SelectableAction:
        observations = [
            f"failure_type={failure_type}",
            f"probe_fingerprint={state_fingerprint}",
        ]
        if recent_logs:
            observations.append(recent_logs[-1][:180])

        parameters = {
            "summary": f"Degraded mode escalation for incident {incident_id}",
            "observations": observations,
            "actions_taken": actions_taken,
            "uncertainty": "LLM unavailable or low-confidence planning path.",
        }
        validated = self._context.tools.validate_action_schema(
            "escalate_to_operator",
            parameters,
        )
        normalized_parameters = validated.model_dump()
        action_key = self._executor.action_key("escalate_to_operator", normalized_parameters)
        return SelectableAction(
            action_key=action_key,
            action_id="escalate_to_operator",
            parameters=normalized_parameters,
            value_score=self._context.policy.score(failure_type, action_key),
        )

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
        for attempt in range(self._llm_task_attempts()):
            try:
                diagnosis = await self._diagnoser.diagnose(
                    failure_type_hint=hint,
                    recent_logs=recent_logs,
                    probes=probes,
                )
                await self._emit_span("loop.diagnose", span_started)
                return diagnosis
            except (LLMUnavailableError, LLMOutputValidationError) as exc:
                await self._context.events.publish(
                    Event(
                        event_type="llm.diagnose_retry",
                        payload={
                            "failure_type": failure_type,
                            "attempt": attempt + 1,
                            "error": str(exc),
                        },
                    )
                )
                await asyncio.sleep(0.2)
                continue
        await self._emit_span("loop.diagnose", span_started, attributes={"status": "error"})
        return None

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
        for attempt in range(self._llm_task_attempts()):
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
            except (LLMUnavailableError, LLMOutputValidationError) as exc:
                await self._context.events.publish(
                    Event(
                        event_type="llm.plan_retry",
                        payload={
                            "failure_type": failure_type,
                            "attempt": attempt + 1,
                            "error": str(exc),
                        },
                    )
                )
                await asyncio.sleep(0.2)
        await self._emit_span("loop.plan", span_started, attributes={"status": "error"})
        return (
            [],
            PlannerTrace(
                planner_input_fields=planner_fields,
                latency_ms=max(0.0, (perf_counter() - span_started) * 1000.0),
                token_estimate=0,
            ),
        )

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

    def _llm_task_attempts(self) -> int:
        return max(2, self._context.settings.llm_retries + 1)

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
                if self._context.settings.readonly_replan_enabled:
                    diagnostic_steps = [step for step in validated_steps if not step.executable]
                    plan_class = (
                        "read_only_throughout"
                        if all(not step.executable for step in validated_steps)
                        else "read_then_mutate"
                    )
                    needs_diagnosis.append(
                        DiagnosePlan(
                            plan_id=plan_id,
                            plan_class=plan_class,
                            steps=diagnostic_steps[
                                : self._context.settings.max_readonly_steps_per_round
                            ],
                        )
                    )
                    continue

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
            "must_return_executable_or_escalate": force_executable,
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

    def _readonly_read_app_file_allowed(self, parameters: dict[str, Any]) -> bool:
        raw_path = str(parameters.get("path", "")).strip().lstrip("./")
        return raw_path in _READONLY_REPLAN_ALLOWED_APP_FILES

    def _sanitize_read_observation(self, *, data: dict[str, Any], max_chars: int) -> str:
        if max_chars <= 0:
            return ""
        serialized = json.dumps(data, sort_keys=True, default=str)
        for pattern in _SECRET_PATTERNS:
            serialized = pattern.sub("[REDACTED]", serialized)
        max_per_read = self._context.settings.readonly_replan_max_chars_per_read
        hard_cap = max(1, min(max_chars, max_per_read))
        return serialized[:hard_cap]

    async def _run_readonly_replan_rounds(
        self,
        *,
        incident_id: str,
        failure_type: str,
        state_fingerprint: str,
        diagnosis: Diagnosis,
        permitted_primitives: frozenset[str],
        failed_action_keys: set[str],
        attempt_number: int,
        initial_build: CandidateBuildResult,
        recent_rejections: deque[CandidateRejection],
    ) -> ReadonlyReplanOutcome:
        max_rounds = self._context.settings.max_readonly_rounds_per_incident
        if max_rounds <= 0:
            escalation_action = self._build_escalation_action(
                failure_type=failure_type,
                incident_id=incident_id,
                state_fingerprint=state_fingerprint,
                recent_logs=[],
                actions_taken=[],
            )
            return ReadonlyReplanOutcome(
                plans=[],
                build_result=initial_build,
                replan_attempt=0,
                cache_hit=False,
                escalation_action=escalation_action,
                planner_trace=PlannerTrace(
                    planner_input_fields=(),
                    latency_ms=0.0,
                    token_estimate=0,
                ),
            )

        current_build = initial_build
        round_history: list[dict[str, Any]] = []
        seen_observations: set[str] = set()
        used_read_actions: set[str] = set()
        latest_plans: list[Any] = []
        latest_trace = PlannerTrace(planner_input_fields=(), latency_ms=0.0, token_estimate=0)
        started_at = perf_counter()
        stop_reason = "round_cap_exhausted"

        for round_index in range(1, max_rounds + 1):
            elapsed = perf_counter() - started_at
            if elapsed > self._context.settings.readonly_replan_max_incident_seconds:
                stop_reason = "readonly_time_budget_exhausted"
                break
            if not current_build.needs_diagnosis:
                stop_reason = "no_diagnosis_plan"
                break

            diagnose_plan = current_build.needs_diagnosis[0]
            round_outputs: list[dict[str, Any]] = []
            round_rejections: list[CandidateRejection] = []
            new_info = 0

            for step in diagnose_plan.steps[: self._context.settings.max_readonly_steps_per_round]:
                if step.action_key in used_read_actions:
                    round_outputs.append(
                        {
                            "action_key": step.action_key,
                            "action_id": step.action_id,
                            "step_index": step.step_index,
                            "status": "skipped_repeated_action",
                            "shield": "not_checked",
                            "output_size": 0,
                            "step_cost_ms": 0.0,
                        }
                    )
                    continue

                if step.action_id == "read_app_file" and not self._readonly_read_app_file_allowed(
                    step.parameters
                ):
                    rejection = CandidateRejection(
                        plan_id=step.plan_id,
                        step_index=step.step_index,
                        action_key=step.action_key,
                        reason="readonly_path_not_allowed",
                        detail="read_app_file path is outside readonly round allowlist",
                    )
                    round_rejections.append(rejection)
                    round_outputs.append(
                        {
                            "action_key": step.action_key,
                            "action_id": step.action_id,
                            "step_index": step.step_index,
                            "status": "blocked",
                            "shield": "path_allowlist_blocked",
                            "output_size": 0,
                            "step_cost_ms": 0.0,
                        }
                    )
                    continue

                shield_pre = self._shield.precheck_candidate(
                    action_id=step.action_id,
                    parameters=step.parameters,
                    attempt_number=attempt_number,
                    has_recent_read_observation=True,
                )
                if not shield_pre.allowed:
                    rejection = CandidateRejection(
                        plan_id=step.plan_id,
                        step_index=step.step_index,
                        action_key=step.action_key,
                        reason=f"shield_pre_blocked:{shield_pre.reason or 'unknown'}",
                        detail=(
                            f"Readonly round pre-check blocked step {step.action_id} "
                            f"with reason {shield_pre.reason or 'unknown'}"
                        ),
                    )
                    round_rejections.append(rejection)
                    round_outputs.append(
                        {
                            "action_key": step.action_key,
                            "action_id": step.action_id,
                            "step_index": step.step_index,
                            "status": "blocked",
                            "shield": "pre_blocked",
                            "output_size": 0,
                            "step_cost_ms": 0.0,
                        }
                    )
                    continue

                shield_post = self._shield.postcheck_candidate(
                    action_id=step.action_id,
                    parameters=step.parameters,
                    parameter_state=shield_pre.parameter_state,
                )
                if not shield_post.allowed:
                    rejection = CandidateRejection(
                        plan_id=step.plan_id,
                        step_index=step.step_index,
                        action_key=step.action_key,
                        reason=f"shield_post_blocked:{shield_post.reason or 'unknown'}",
                        detail=(
                            f"Readonly round post-check blocked step {step.action_id} "
                            f"with reason {shield_post.reason or 'unknown'}"
                        ),
                    )
                    round_rejections.append(rejection)
                    round_outputs.append(
                        {
                            "action_key": step.action_key,
                            "action_id": step.action_id,
                            "step_index": step.step_index,
                            "status": "blocked",
                            "shield": "post_blocked",
                            "output_size": 0,
                            "step_cost_ms": 0.0,
                        }
                    )
                    continue

                step_started = perf_counter()
                result = await self._context.tools.execute(
                    step.action_id,
                    step.parameters,
                    allowed_primitives=permitted_primitives,
                )
                step_cost_ms = max(0.0, (perf_counter() - step_started) * 1000.0)
                used_read_actions.add(step.action_key)

                consumed_chars = sum(
                    len(str(item.get("quoted_data", ""))) for item in round_outputs
                )
                remaining_chars = max(
                    0,
                    self._context.settings.readonly_replan_max_chars_per_round - consumed_chars,
                )
                quoted_data = self._sanitize_read_observation(
                    data=result.data,
                    max_chars=remaining_chars,
                )
                digest = (
                    hashlib.sha256(quoted_data.encode("utf-8")).hexdigest()
                    if quoted_data
                    else ""
                )
                is_new = bool(quoted_data) and digest not in seen_observations
                if is_new:
                    new_info += 1
                    seen_observations.add(digest)

                round_outputs.append(
                    {
                        "action_key": step.action_key,
                        "action_id": step.action_id,
                        "step_index": step.step_index,
                        "status": "executed",
                        "shield": "allowed",
                        "output_size": len(quoted_data),
                        "quoted_data": quoted_data,
                        "step_cost_ms": step_cost_ms,
                    }
                )

                reward_components = score_outcome(
                    verifier_healthy=False,
                    duration_seconds=step_cost_ms / 1000.0,
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
                        "readonly_round": str(round_index),
                    },
                    candidate_set=[step.action_key],
                    chosen_action=step.action_key,
                    propensities={step.action_key: 1.0},
                    verifier_healthy=None,
                    reward_components=reward_components,
                    shield_outcome="allowed",
                    step_cost_ms=step_cost_ms,
                    recovery_result="diagnosis_round",
                )

            if round_rejections:
                recent_rejections.extend(round_rejections)
                current_build = CandidateBuildResult(
                    candidates=current_build.candidates,
                    rejections=[*current_build.rejections, *round_rejections],
                    needs_diagnosis=current_build.needs_diagnosis,
                )

            await self._context.events.publish(
                Event(
                    event_type="agent.readonly_diagnosis_round",
                    payload={
                        "incident_id": incident_id,
                        "failure_type": failure_type,
                        "round": round_index,
                        "plan_id": diagnose_plan.plan_id,
                        "plan_class": diagnose_plan.plan_class,
                        "steps": round_outputs,
                        "new_observations": new_info,
                        "allowed_read_app_file_paths": sorted(_READONLY_REPLAN_ALLOWED_APP_FILES),
                    },
                )
            )
            round_history.append(
                {
                    "round": round_index,
                    "plan_id": diagnose_plan.plan_id,
                    "plan_class": diagnose_plan.plan_class,
                    "steps": round_outputs,
                    "new_observations": new_info,
                }
            )

            if new_info <= 0:
                stop_reason = "readonly_no_progress"
                break

            fresh_logs = await self._diagnoser.collect_logs()
            fresh_probes = await self._diagnoser.collect_probes()
            fresh_fingerprint = probe_fingerprint(fresh_probes, fresh_logs)
            force_executable = round_index >= max_rounds
            constraints = self._planner_constraints_payload(
                state_fingerprint=fresh_fingerprint["probes"],
                recent_rejections=recent_rejections,
                readonly_round=round_index,
                max_rounds=max_rounds,
                force_executable=force_executable,
                read_observations=[
                    {
                        "action_key": item["action_key"],
                        "step_index": item["step_index"],
                        "quoted_data": item.get("quoted_data", ""),
                    }
                    for item in round_outputs
                    if item.get("status") == "executed"
                ],
                state_snapshot=fresh_probes,
                state_version=hashlib.sha256(
                    fresh_fingerprint["probes"].encode("utf-8")
                ).hexdigest()[:16],
            )

            latest_plans, latest_trace = await self._safe_propose_plans(
                diagnosis=diagnosis,
                incident_id=incident_id,
                failure_type=failure_type,
                permitted_primitives=permitted_primitives,
                failed_action_keys=failed_action_keys,
                constraints=constraints,
            )
            current_build = await self._build_candidates_from_plans(
                failure_type=failure_type,
                permitted_primitives=permitted_primitives,
                plans=latest_plans,
                mutating_only=True,
                attempt_number=attempt_number,
                has_recent_read_observation=True,
            )
            recent_rejections.extend(current_build.rejections)
            if current_build.candidates:
                return ReadonlyReplanOutcome(
                    plans=latest_plans,
                    build_result=current_build,
                    replan_attempt=round_index,
                    cache_hit=False,
                    escalation_action=None,
                    planner_trace=latest_trace,
                )

        escalation_action = self._build_escalation_action(
            failure_type=failure_type,
            incident_id=incident_id,
            state_fingerprint=state_fingerprint,
            recent_logs=[f"readonly_replan_stop_reason={stop_reason}"],
            actions_taken=list(sorted(used_read_actions))[-5:],
        )
        await self._context.events.publish(
            Event(
                event_type="agent.readonly_replan_escalated",
                payload={
                    "incident_id": incident_id,
                    "failure_type": failure_type,
                    "reason": stop_reason,
                    "round_history": round_history,
                },
            )
        )
        current_build = CandidateBuildResult(
            candidates=current_build.candidates,
            rejections=[
                *current_build.rejections,
                CandidateRejection(
                    plan_id="readonly-replan",
                    step_index=-1,
                    action_key=escalation_action.action_key,
                    reason=stop_reason,
                    detail="Readonly diagnose/replan exhausted before executable plan",
                ),
            ],
            needs_diagnosis=current_build.needs_diagnosis,
        )
        recent_rejections.extend(current_build.rejections[-1:])
        return ReadonlyReplanOutcome(
            plans=latest_plans,
            build_result=current_build,
            replan_attempt=max_rounds,
            cache_hit=False,
            escalation_action=escalation_action,
            planner_trace=latest_trace,
        )

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
            elif chosen_action.startswith("escalate_to_operator"):
                recovery_result = "escalated"
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
