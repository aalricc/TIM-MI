from __future__ import annotations

import pytest

from mi_agent.llm.client import FakeLLMClient
from mi_agent.runtime import build_agent
from mi_agent.tools.schemas import CandidatePlan

pytestmark = [pytest.mark.asyncio(), pytest.mark.phase5]


async def test_candidate_builder_includes_wait_when_planned(test_settings) -> None:
    agent, context = build_agent(test_settings, FakeLLMClient())

    plans = [
        CandidatePlan.model_validate(
            {
                "name": "wait-first",
                "rationale": "allow transient lock to clear",
                "steps": [{"action_id": "wait", "parameters": {"seconds": 1.0}}],
            }
        ),
        CandidatePlan.model_validate(
            {
                "name": "readonly-other",
                "rationale": "read only",
                "steps": [{"action_id": "read_app_file", "parameters": {"path": "schema.sql"}}],
            }
        ),
    ]

    build_result = await agent._build_candidates_from_plans(
        failure_type="db_file_missing",
        permitted_primitives=context.tools.primitive_ids,
        plans=plans,
        mutating_only=True,
        attempt_number=1,
        has_recent_read_observation=True,
    )

    action_ids = {candidate.action_id for candidate in build_result.candidates}
    assert "wait" in action_ids
    assert "read_app_file" not in action_ids


async def test_candidate_builder_applies_shield_intersection(test_settings) -> None:
    agent, context = build_agent(test_settings, FakeLLMClient())

    plans = [
        CandidatePlan.model_validate(
            {
                "name": "restart",
                "rationale": "service bounce",
                "steps": [
                    {
                        "action_id": "restart_service",
                        "parameters": {"service_name": "target-app"},
                    }
                ],
            }
        )
    ]

    build_result = await agent._build_candidates_from_plans(
        failure_type="db_file_missing",
        permitted_primitives=context.tools.primitive_ids,
        plans=plans,
        mutating_only=True,
        attempt_number=1,
        has_recent_read_observation=False,
    )

    assert build_result.candidates == []


async def test_candidate_builder_rejects_non_executable_first_step_even_when_flag_enabled(
    test_settings,
) -> None:
    settings = test_settings.model_copy(update={"readonly_replan_enabled": True})
    agent, context = build_agent(settings, FakeLLMClient())

    plans = [
        CandidatePlan.model_validate(
            {
                "name": "readonly-first",
                "rationale": "inspect first",
                "steps": [
                    {
                        "action_id": "read_app_file",
                        "parameters": {"path": "schema.sql"},
                    },
                    {
                        "action_id": "apply_schema",
                        "parameters": {
                            "db_path": "data/app.db",
                            "schema_path": "schema.sql",
                        },
                    },
                ],
            }
        )
    ]

    build_result = await agent._build_candidates_from_plans(
        failure_type="db_file_missing",
        permitted_primitives=context.tools.primitive_ids,
        plans=plans,
        mutating_only=True,
        attempt_number=1,
        has_recent_read_observation=True,
    )

    assert build_result.candidates == []
    assert len(build_result.rejections) == 1
    assert build_result.rejections[0].reason == "first_step_not_executable"
    assert build_result.needs_diagnosis == []


async def test_candidate_builder_rejects_first_step_not_executable_when_flag_disabled(
    test_settings,
) -> None:
    settings = test_settings.model_copy(update={"readonly_replan_enabled": False})
    agent, context = build_agent(settings, FakeLLMClient())

    plans = [
        CandidatePlan.model_validate(
            {
                "name": "readonly-first",
                "rationale": "inspect first",
                "steps": [
                    {
                        "action_id": "read_app_file",
                        "parameters": {"path": "schema.sql"},
                    }
                ],
            }
        )
    ]

    build_result = await agent._build_candidates_from_plans(
        failure_type="db_file_missing",
        permitted_primitives=context.tools.primitive_ids,
        plans=plans,
        mutating_only=True,
        attempt_number=1,
        has_recent_read_observation=True,
    )

    assert build_result.candidates == []
    assert build_result.needs_diagnosis == []
    assert build_result.rejections
    assert build_result.rejections[0].reason == "first_step_not_executable"
