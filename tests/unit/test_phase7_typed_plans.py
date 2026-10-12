from __future__ import annotations

import pytest
from pydantic import ValidationError

from mi_agent.llm.client import FakeLLMClient
from mi_agent.runtime import build_agent
from mi_agent.tools.schemas import CandidatePlan

pytestmark = pytest.mark.phase7


def test_candidate_plan_step_keeps_typed_preconditions_and_expected_postconditions() -> None:
    plan = CandidatePlan.model_validate(
        {
            "name": "repair",
            "rationale": "repair db",
            "steps": [
                {
                    "action_id": "apply_schema",
                    "parameters": {"db_path": "data/app.db", "schema_path": "schema.sql"},
                    "preconditions": ["probe:db_file.exists=false"],
                    "expected_postconditions": ["probe:schema_ok=true"],
                }
            ],
        }
    )

    step = plan.steps[0]
    assert step.preconditions == ["probe:db_file.exists=false"]
    assert step.expected_postconditions == ["probe:schema_ok=true"]


def test_candidate_plan_rejects_looping_steps() -> None:
    with pytest.raises(ValidationError):
        CandidatePlan.model_validate(
            {
                "name": "looping",
                "rationale": "retry forever",
                "steps": [
                    {
                        "action_id": "wait",
                        "parameters": {"seconds": 1.0},
                        "preconditions": ["probe:db_ping=false"],
                        "expected_postconditions": ["probe:db_ping=true"],
                    },
                    {
                        "action_id": "wait",
                        "parameters": {"seconds": 1.0},
                        "preconditions": ["probe:db_ping=false"],
                        "expected_postconditions": ["probe:db_ping=true"],
                    },
                ],
            }
        )


@pytest.mark.asyncio()
async def test_candidate_builder_rejects_plan_when_any_step_fails_static_validation(
    test_settings,
) -> None:
    agent, context = build_agent(test_settings, FakeLLMClient())

    plans = [
        CandidatePlan.model_validate(
            {
                "name": "two-step",
                "rationale": "repair then write marker",
                "steps": [
                    {
                        "action_id": "apply_schema",
                        "parameters": {
                            "db_path": "data/app.db",
                            "schema_path": "schema.sql",
                        },
                        "preconditions": ["probe:db_file.exists=false"],
                        "expected_postconditions": ["probe:schema_ok=true"],
                    },
                    {
                        "action_id": "write_app_file",
                        "parameters": {
                            "path": "../../outside.txt",
                            "content": "unsafe",
                        },
                        "preconditions": ["probe:schema_ok=true"],
                        "expected_postconditions": ["probe:db_ping=true"],
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
