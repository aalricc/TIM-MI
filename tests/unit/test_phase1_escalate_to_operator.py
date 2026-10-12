from __future__ import annotations

import pytest

from mi_agent.llm.client import FakeLLMClient
from mi_agent.runtime import build_agent

pytestmark = pytest.mark.phase1


def _escalation_params() -> dict[str, object]:
    return {
        "summary": "Need operator confirmation before destructive restoration",
        "observations": ["backup freshness ambiguous", "service still degraded"],
        "actions_taken": ["check_db_integrity", "list_backups"],
        "uncertainty": "Cannot decide between stale-restore and no-op",
    }


@pytest.mark.asyncio()
async def test_escalate_to_operator_contract_and_result_shape(test_settings) -> None:
    _agent, context = build_agent(test_settings, FakeLLMClient())

    contract = context.tools.primitive_parameter_contracts()["escalate_to_operator"]
    assert contract["risk_tier"] == "terminal"
    assert contract["mutating"] is False
    assert contract["reversible"] is False
    assert isinstance(contract["description"], str)
    assert contract["description"].strip() != ""

    result = await context.tools.execute("escalate_to_operator", _escalation_params())
    assert result.data["escalated"] is True
    assert result.data["report"]["summary"].startswith("Need operator")

    event_types = [event.event_type for event in context.events.events]
    assert "operator.escalated" in event_types


@pytest.mark.asyncio()
async def test_agent_stops_autonomous_handling_after_escalation(test_settings) -> None:
    llm = FakeLLMClient()
    llm.queue_response(
        "diagnose",
        {
            "failure_type": "db_corrupt",
            "hypotheses": ["data continuity risk"],
            "evidence": ["probe mismatch"],
        },
    )
    llm.queue_response(
        "propose_plans",
        [
            {
                "name": "escalate",
                "rationale": "uncertain outcomes",
                "steps": [
                    {
                        "action_id": "escalate_to_operator",
                        "parameters": _escalation_params(),
                    }
                ],
            }
        ],
    )

    agent, context = build_agent(test_settings, llm)

    async def _fail_if_called(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("verifier should not run for escalate_to_operator")

    context.verifier.verify = _fail_if_called  # type: ignore[method-assign]

    await agent._recover_loop()

    event_types = [event.event_type for event in context.events.events]
    assert "operator.escalated" in event_types
    assert "agent.incident_unresolved" in event_types
    assert "verifier.result" not in event_types
