from __future__ import annotations

import pytest

from mi_agent.config import MISettings
from mi_agent.llm.client import FakeLLMClient
from mi_agent.runtime import build_agent

pytestmark = [pytest.mark.asyncio(), pytest.mark.phase7]


class CaptureFakeLLM(FakeLLMClient):
    def __init__(self) -> None:
        super().__init__()
        self.plan_constraints: list[dict[str, object]] = []

    async def propose_plans(
        self,
        *,
        diagnosis,
        available_actions,
        action_parameter_contracts,
        recalled_skills,
        failed_action_keys=(),
        constraints=None,
    ):
        self.plan_constraints.append(dict(constraints or {}))
        return await super().propose_plans(
            diagnosis=diagnosis,
            available_actions=available_actions,
            action_parameter_contracts=action_parameter_contracts,
            recalled_skills=recalled_skills,
            failed_action_keys=failed_action_keys,
            constraints=constraints,
        )


def _missing_db_diagnosis() -> dict[str, object]:
    return {
        "failure_type": "db_file_missing",
        "hypotheses": ["database file missing"],
        "evidence": ["log signature matched"],
    }


def _apply_schema_plan() -> dict[str, object]:
    return {
        "name": "recover",
        "rationale": "restore db",
        "steps": [
            {
                "action_id": "apply_schema",
                "parameters": {"db_path": "data/app.db", "schema_path": "schema.sql"},
                "preconditions": ["db missing"],
                "expected_postconditions": ["db restored"],
            }
        ],
    }


async def test_planner_constraints_and_no_readonly_replan_events(
    test_settings: MISettings,
) -> None:
    llm = CaptureFakeLLM()
    llm.queue_response("diagnose", _missing_db_diagnosis())
    llm.queue_response("propose_plans", [_apply_schema_plan()])

    agent, context = build_agent(test_settings, llm)
    context.verifier.health_endpoint_ok = lambda: _async_true()  # type: ignore[method-assign]

    test_settings.db_file.unlink(missing_ok=True)
    await agent._recover_loop()

    assert llm.plan_constraints
    first_constraints = llm.plan_constraints[0]
    assert first_constraints.get("must_return_executable_action") is False

    event_types = [event.event_type for event in context.events.events]
    assert "agent.readonly_diagnosis_round" not in event_types
    assert "agent.readonly_replan_exhausted" not in event_types


async def _async_true() -> bool:
    return True
