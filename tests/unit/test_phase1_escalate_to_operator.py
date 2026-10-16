from __future__ import annotations

import pytest

from mi_agent.errors import LLMPermanentError
from mi_agent.llm.client import FakeLLMClient
from mi_agent.runtime import build_agent

pytestmark = pytest.mark.phase1


@pytest.mark.asyncio()
async def test_operator_escalation_primitive_removed(test_settings) -> None:
    _agent, context = build_agent(test_settings, FakeLLMClient())
    assert "escalate_to_operator" not in context.tools.primitive_parameter_contracts()


@pytest.mark.asyncio()
async def test_agent_stops_on_permanent_llm_error_without_operator_event(test_settings) -> None:
    llm = FakeLLMClient()

    async def _fail_preflight() -> dict[str, str]:
        raise LLMPermanentError("401 unauthorized")

    llm.preflight = _fail_preflight  # type: ignore[method-assign]
    agent, context = build_agent(test_settings, llm)
    await agent._recover_loop()

    event_types = [event.event_type for event in context.events.events]
    assert "agent.llm_permanent_error" in event_types
    assert "operator.escalated" not in event_types
