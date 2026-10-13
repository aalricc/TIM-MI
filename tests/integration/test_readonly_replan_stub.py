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


def _write_signature_log(settings: MISettings) -> None:
    settings.app_log.write_text(
        "sqlite3.OperationalError: unable to open database file\n",
        encoding="utf-8",
    )


def _readonly_plan(path: str = "schema.sql") -> dict[str, object]:
    return {
        "name": "readonly-diagnose",
        "rationale": "gather data first",
        "steps": [{"action_id": "read_app_file", "parameters": {"path": path}}],
    }


async def test_readonly_replan_feature_flag_no_longer_triggers_extra_rounds(
    test_settings: MISettings,
) -> None:
    settings = test_settings.model_copy(
        update={
            "readonly_replan_enabled": True,
            "max_readonly_rounds_per_incident": 2,
            "max_attempts_per_incident": 1,
        }
    )
    llm = CaptureFakeLLM()
    llm.queue_response("diagnose", _missing_db_diagnosis())
    llm.queue_response("propose_plans", [_readonly_plan()])

    agent, context = build_agent(settings, llm)

    settings.db_file.unlink(missing_ok=True)
    _write_signature_log(settings)
    await agent._recover_loop()

    event_types = [event.event_type for event in context.events.events]
    assert "agent.unresolved_autonomous" in event_types
    assert "agent.readonly_diagnosis_round" not in event_types
    assert "agent.readonly_replan_exhausted" not in event_types


async def test_planner_constraints_still_request_executable_actions(
    test_settings: MISettings,
) -> None:
    settings = test_settings.model_copy(update={"max_attempts_per_incident": 1})
    llm = CaptureFakeLLM()
    llm.queue_response("diagnose", _missing_db_diagnosis())
    llm.queue_response("propose_plans", [_readonly_plan()])

    agent, _ = build_agent(settings, llm)

    settings.db_file.unlink(missing_ok=True)
    _write_signature_log(settings)
    await agent._recover_loop()

    assert llm.plan_constraints
    first_constraints = llm.plan_constraints[0]
    assert first_constraints.get("must_return_executable_action") is False
