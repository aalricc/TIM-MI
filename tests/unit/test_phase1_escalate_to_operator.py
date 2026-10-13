from __future__ import annotations

import pytest

from mi_agent.llm.client import FakeLLMClient
from mi_agent.runtime import build_agent

pytestmark = pytest.mark.phase1


@pytest.mark.asyncio()
async def test_operator_escalation_primitive_removed(test_settings) -> None:
    _agent, context = build_agent(test_settings, FakeLLMClient())
    assert "escalate_to_operator" not in context.tools.primitive_parameter_contracts()


@pytest.mark.asyncio()
async def test_agent_ends_with_unresolved_autonomous_when_no_viable_candidates(
    test_settings,
) -> None:
    llm = FakeLLMClient()
    llm.queue_response(
        "diagnose",
        {
            "failure_type": "db_corrupt",
            "hypotheses": ["continuity risk"],
            "evidence": ["probe mismatch"],
        },
    )
    llm.queue_response(
        "propose_plans",
        [
            {
                "name": "readonly-only",
                "rationale": "observe only",
                "steps": [{"action_id": "read_app_log", "parameters": {"lines": 10}}],
            }
        ],
    )

    settings = test_settings.model_copy(update={"max_attempts_per_incident": 1})
    agent, context = build_agent(settings, llm)
    await agent._recover_loop()

    event_types = [event.event_type for event in context.events.events]
    assert "agent.unresolved_autonomous" in event_types
    assert "operator.escalated" not in event_types
