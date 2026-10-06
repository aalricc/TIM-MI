from __future__ import annotations

from typing import Any

from mi_agent.errors import LLMOutputValidationError
from mi_agent.failure_types import normalize_failure_type
from mi_agent.llm.client import LLMClient
from mi_agent.skills.model import SkillRecord, SkillStep
from mi_agent.tools.executor import ToolExecutor
from mi_agent.tools.schemas import SkillProposal


class SkillDistiller:
    def __init__(self, llm_client: LLMClient, tool_executor: ToolExecutor) -> None:
        self._llm_client = llm_client
        self._tool_executor = tool_executor

    async def distill(
        self,
        *,
        failure_type: str,
        episode_actions: list[tuple[str, dict[str, Any], bool]],
        trigger_fingerprint: dict[str, str],
        allowed_primitives: frozenset[str],
    ) -> SkillRecord | None:
        failure_type = normalize_failure_type(failure_type)
        proposal = await self._llm_client.distill_skill(
            failure_type=failure_type,
            episode_actions=episode_actions,
            trigger_fingerprint=trigger_fingerprint,
        )
        normalized_steps = self._validate_and_normalize_proposal(
            proposal=proposal,
            episode_actions=episode_actions,
            allowed_primitives=allowed_primitives,
        )
        return SkillRecord(
            name=proposal.name,
            description=proposal.description,
            failure_type=failure_type,
            trigger_fingerprint=trigger_fingerprint,
            preconditions=proposal.preconditions,
            steps=normalized_steps,
            expected_postcondition=proposal.expected_postcondition,
        )

    def _validate_and_normalize_proposal(
        self,
        *,
        proposal: SkillProposal,
        episode_actions: list[tuple[str, dict[str, Any], bool]],
        allowed_primitives: frozenset[str],
    ) -> list[SkillStep]:
        successful_episode_steps: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
        for action_id, parameters, success in episode_actions:
            if not success:
                continue
            merged = self._tool_executor.with_default_parameters(action_id, parameters)
            validated = self._tool_executor.validate_action_schema(action_id, merged)
            successful_episode_steps.add((action_id, self._stable_dict(validated.model_dump())))

        normalized_steps: list[SkillStep] = []
        for step in proposal.steps:
            if step.action_id not in allowed_primitives:
                raise LLMOutputValidationError(
                    f"Skill step primitive not allowed: {step.action_id}"
                )
            merged = self._tool_executor.with_default_parameters(step.action_id, step.parameters)
            validated = self._tool_executor.validate_action_schema(step.action_id, merged)
            normalized = validated.model_dump()
            key = (step.action_id, self._stable_dict(normalized))
            if key not in successful_episode_steps:
                raise LLMOutputValidationError(
                    f"Skill step not consistent with successful episode: {step.action_id}"
                )
            normalized_steps.append(
                SkillStep(primitive_id=step.action_id, parameters=normalized)
            )
        return normalized_steps

    @staticmethod
    def _stable_dict(value: dict[str, Any]) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((k, repr(v)) for k, v in value.items()))
