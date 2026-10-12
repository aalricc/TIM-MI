from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any

from mi_agent.errors import PrimitiveNotAllowedError
from mi_agent.events.bus import EventBus
from mi_agent.events.models import Event
from mi_agent.skills.model import SkillRecord
from mi_agent.tools.executor import ToolExecutionResult, ToolExecutor, ToolSnapshot


@dataclass(slots=True)
class ActionExecution:
    action_key: str
    success: bool
    snapshot: ToolSnapshot | None
    step_results: list[ToolExecutionResult]


@dataclass(slots=True)
class FailedAttempt:
    state_fingerprint: str
    recorded_at: float


class AgentActionExecutor:
    def __init__(
        self,
        tool_executor: ToolExecutor,
        event_bus: EventBus,
        *,
        failed_action_ttl_seconds: float = 900.0,
        now_fn: Callable[[], float] = monotonic,
    ) -> None:
        self._tool_executor = tool_executor
        self._event_bus = event_bus
        self._failed_attempts: dict[str, FailedAttempt] = {}
        self._failed_action_ttl_seconds = failed_action_ttl_seconds
        self._now = now_fn

    @staticmethod
    def action_key(action_id: str, params: dict[str, Any]) -> str:
        stable = ",".join(f"{k}={v!r}" for k, v in sorted(params.items()))
        return f"{action_id}|{stable}"

    async def execute_primitive(
        self,
        *,
        action_id: str,
        parameters: dict[str, Any],
        allowed_primitives: frozenset[str],
        state_fingerprint: str,
    ) -> ActionExecution:
        action_key = self.action_key(action_id, parameters)
        self._reject_repeat_failures(action_key, state_fingerprint)
        result = await self._tool_executor.execute(
            action_id,
            parameters,
            allowed_primitives=allowed_primitives,
        )
        await self._event_bus.publish(
            Event(event_type="agent.action_executed", payload={"action_key": action_key})
        )
        return ActionExecution(
            action_key=action_key,
            success=True,
            snapshot=result.snapshot,
            step_results=[result],
        )

    async def execute_skill(
        self,
        *,
        skill: SkillRecord,
        allowed_primitives: frozenset[str],
        state_fingerprint: str,
    ) -> ActionExecution:
        action_key = f"skill:{skill.name}:v{skill.version}"
        self._reject_repeat_failures(action_key, state_fingerprint)
        snapshots: list[ToolSnapshot] = []
        results: list[ToolExecutionResult] = []
        for step in skill.steps:
            if step.primitive_id not in allowed_primitives:
                raise PrimitiveNotAllowedError(
                    f"Skill step {step.primitive_id} not permitted for this failure"
                )
            result = await self._tool_executor.execute(
                step.primitive_id,
                step.parameters,
                allowed_primitives=allowed_primitives,
            )
            if result.snapshot is not None:
                snapshots.append(result.snapshot)
            results.append(result)
        await self._event_bus.publish(
            Event(event_type="agent.skill_executed", payload={"skill": skill.name})
        )
        snapshot = snapshots[-1] if snapshots else None
        return ActionExecution(
            action_key=action_key, success=True, snapshot=snapshot, step_results=results
        )

    def mark_failed(self, action_key: str, state_fingerprint: str) -> None:
        self._failed_attempts[action_key] = FailedAttempt(
            state_fingerprint=state_fingerprint,
            recorded_at=self._now(),
        )

    async def rollback_if_needed(self, snapshot: ToolSnapshot | None) -> None:
        if snapshot is None:
            return
        await self._tool_executor.rollback(snapshot)

    def _reject_repeat_failures(self, action_key: str, state_fingerprint: str) -> None:
        previous = self._failed_attempts.get(action_key)
        if previous is None:
            return

        age = self._now() - previous.recorded_at
        if age > self._failed_action_ttl_seconds:
            self._failed_attempts.pop(action_key, None)
            return

        if previous.state_fingerprint != state_fingerprint:
            self._failed_attempts.pop(action_key, None)
            return

        if previous.state_fingerprint == state_fingerprint:
            raise PrimitiveNotAllowedError(
                "Action already failed with same parameters and unchanged system state"
            )
