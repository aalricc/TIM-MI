from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from mi_agent.agent.diagnose import Diagnoser, probe_fingerprint
from mi_agent.agent.executor import AgentActionExecutor
from mi_agent.agent.monitor import MonitorLoop
from mi_agent.agent.selector import EpsilonGreedySelector, SelectableAction
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
from mi_agent.failure_types import UNCLASSIFIED_FAILURE, normalize_failure_type
from mi_agent.llm.client import LLMClient
from mi_agent.policy.store import PolicyStore
from mi_agent.skills.distiller import SkillDistiller
from mi_agent.skills.library import SkillLibrary
from mi_agent.skills.matcher import SkillMatcher
from mi_agent.supervisor.last_resort import LastResortError
from mi_agent.supervisor.supervisor import IncidentLock, Supervisor
from mi_agent.tools.executor import ToolExecutor
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
    skill_library: SkillLibrary
    llm: LLMClient
    selector: EpsilonGreedySelector
    skill_matcher: SkillMatcher
    skill_distiller: SkillDistiller
    alerts: AlertSink


class MIAgent:
    def __init__(self, context: MIContext) -> None:
        self._context = context
        self._diagnoser = Diagnoser(context.llm, context.tools, context.policy, context.settings)
        self._executor = AgentActionExecutor(context.tools, context.events)
        self._incident_guard = asyncio.Lock()
        self._monitor = MonitorLoop(
            interval_seconds=context.settings.monitor_interval_seconds,
            ping_fn=self._ping_db,
            on_failure=self._handle_possible_failure,
            event_bus=context.events,
        )

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

        episode_actions: list[tuple[str, dict[str, Any], bool]] = []
        credited_actions: list[EpisodeAction] = []
        visits = 0
        resolved = False
        permitted_primitives = self._context.tools.primitive_ids

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

                recent_logs = await self._diagnoser.collect_logs()
                probes = await self._diagnoser.collect_probes()
                fingerprint = probe_fingerprint(probes, recent_logs)
                state_fingerprint = fingerprint["probes"]

                matched_skill = self._match_skill_without_llm(incident_failure_type, fingerprint)
                if matched_skill is None:
                    matched_skill = await self._safe_match_with_llm(
                        incident_failure_type,
                        fingerprint,
                    )
                if matched_skill is not None and incident_failure_type == _UNCLASSIFIED_FAILURE:
                    incident_failure_type = normalize_failure_type(matched_skill.failure_type)
                    incident.failure_type = incident_failure_type

                action_selected: SelectableAction | None = None
                skill_used = False

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
                    inferred_failure_type = self._normalize_failure_type(diagnosis.failure_type)
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
                    else:
                        failed_keys = {
                            action.action_key for action in credited_actions if not action.success
                        }

                        plans = await self._safe_propose_plans(
                            diagnosis=diagnosis,
                            failure_type=incident_failure_type,
                            permitted_primitives=permitted_primitives,
                            failed_action_keys=failed_keys,
                        )
                        candidates = await self._build_candidates_from_plans(
                            failure_type=incident_failure_type,
                            permitted_primitives=permitted_primitives,
                            plans=plans,
                            mutating_only=True,
                        )
                        if not candidates:
                            await self._context.events.publish(
                                Event(
                                    event_type="agent.no_viable_candidates",
                                    payload={"failure_type": incident_failure_type},
                                )
                            )
                            visits += 1
                            continue
                        if failed_keys:
                            filtered = [
                                candidate
                                for candidate in candidates
                                if candidate.action_key not in failed_keys
                            ]
                            candidates = filtered

                        if not candidates:
                            await self._context.events.publish(
                                Event(
                                    event_type="agent.no_viable_candidates",
                                    payload={"failure_type": incident_failure_type},
                                )
                            )
                            visits += 1
                            continue

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

                started = datetime.now(tz=UTC)
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
                except ToolError as exc:
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
                        credited_actions.append(
                            EpisodeAction(
                                action_key=action_selected.action_key,
                                reward=-1.0,
                                duration_seconds=0.0,
                                success=False,
                            )
                        )
                    visits += 1
                    continue

                verify_result = await self._context.verifier.verify(
                    failure_type=incident_failure_type,
                )
                ended = datetime.now(tz=UTC)
                duration = (ended - started).total_seconds()

                if verify_result.healthy:
                    reward = self._success_reward(duration, gate.attempts)
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
                reward = -1.0
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
        failure_type = normalize_failure_type(failure_type)
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
            learned_skill = self._context.skill_library.register(skill)
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
        failure_type = normalize_failure_type(failure_type)
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

    async def _safe_match_with_llm(
        self,
        failure_type: str,
        fingerprint: dict[str, str],
    ) -> Any | None:
        if failure_type == _UNCLASSIFIED_FAILURE:
            return None
        try:
            return await self._context.skill_matcher.match(
                failure_type=failure_type,
                trigger_fingerprint=fingerprint,
            )
        except (LLMUnavailableError, LLMOutputValidationError) as exc:
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
        hint = None if failure_type == _UNCLASSIFIED_FAILURE else failure_type
        for attempt in range(self._llm_task_attempts()):
            try:
                return await self._diagnoser.diagnose(
                    failure_type_hint=hint,
                    recent_logs=recent_logs,
                    probes=probes,
                )
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
        return None

    async def _safe_propose_plans(
        self,
        *,
        diagnosis: Diagnosis,
        failure_type: str,
        permitted_primitives: frozenset[str],
        failed_action_keys: set[str],
    ) -> list[Any]:
        for attempt in range(self._llm_task_attempts()):
            try:
                return await self._context.llm.propose_plans(
                    diagnosis=diagnosis,
                    available_actions=sorted(permitted_primitives),
                    action_parameter_contracts=self._context.tools.primitive_parameter_contracts(),
                    recalled_skills=[
                        skill.name for skill in self._context.skill_library.all(failure_type)
                    ],
                    failed_action_keys=sorted(failed_action_keys),
                )
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
        return []

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
    ) -> list[SelectableAction]:
        candidates: list[SelectableAction] = []
        for plan in plans:
            if not plan.steps:
                continue
            step = plan.steps[0]
            if step.action_id not in permitted_primitives:
                continue
            if mutating_only and not self._context.tools.is_mutating(step.action_id):
                continue
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
                await self._context.events.publish(
                    Event(
                        event_type="llm.plan_invalid_step",
                        payload={
                            "failure_type": failure_type,
                            "action_id": step.action_id,
                            "error": str(exc),
                        },
                    )
                )
                continue
            parameters = validated.model_dump()
            action_key = self._executor.action_key(step.action_id, parameters)
            candidates.append(
                SelectableAction(
                    action_key=action_key,
                    action_id=step.action_id,
                    parameters=parameters,
                    value_score=self._context.policy.score(failure_type, action_key),
                )
            )
        return candidates

    @staticmethod
    def _normalize_failure_type(raw: str) -> str:
        return normalize_failure_type(raw)

    @staticmethod
    def _success_reward(duration_seconds: float, attempts: int) -> float:
        return max(0.5, 10.0 - attempts - (duration_seconds / 60.0))
