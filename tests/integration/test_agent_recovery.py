from __future__ import annotations

import pytest

from mi_agent.errors import LLMPermanentError
from mi_agent.llm.client import FakeLLMClient
from mi_agent.runtime import build_agent


def _make_diagnosis() -> dict[str, object]:
    return {
        "failure_type": "db_file_missing",
        "hypotheses": ["database file missing"],
        "evidence": ["log signature matched"],
    }


def _apply_schema_step() -> dict[str, object]:
    return {
        "action_id": "apply_schema",
        "parameters": {"db_path": "data/app.db", "schema_path": "schema.sql"},
        "preconditions": ["db missing"],
        "expected_postconditions": ["db restored"],
    }


@pytest.mark.asyncio()
async def test_recovers_missing_db_with_fake_llm_plan(test_settings) -> None:
    llm = FakeLLMClient()
    llm.queue_response("diagnose", _make_diagnosis())
    llm.queue_response(
        "propose_plans",
        [{"name": "recover", "rationale": "apply schema", "steps": [_apply_schema_step()]}],
    )

    agent, context = build_agent(test_settings, llm)
    context.verifier.health_endpoint_ok = lambda: _async_true()  # type: ignore[method-assign]

    test_settings.db_file.unlink()
    await agent._recover_loop()

    assert test_settings.db_file.exists()
    event_types = [event.event_type for event in context.events.events]
    assert "agent.recovered" in event_types


@pytest.mark.asyncio()
async def test_permanent_llm_failure_stops_incident(test_settings) -> None:
    llm = FakeLLMClient()

    async def _fail_preflight() -> dict[str, str]:
        raise LLMPermanentError("401 unauthorized")

    llm.preflight = _fail_preflight  # type: ignore[method-assign]

    agent, context = build_agent(test_settings, llm)
    await agent._recover_loop()

    event_types = [event.event_type for event in context.events.events]
    assert "agent.llm_permanent_error" in event_types
    assert "operator.escalated" not in event_types


@pytest.mark.asyncio()
async def test_unregistered_action_is_rejected_and_replanned(test_settings) -> None:
    llm = FakeLLMClient()
    llm.queue_response("diagnose", _make_diagnosis())
    llm.queue_response(
        "propose_plans",
        [
            {
                "name": "bad",
                "rationale": "not allowlisted",
                "steps": [
                    {
                        "action_id": "not_registered_action",
                        "parameters": {},
                        "preconditions": ["x"],
                        "expected_postconditions": ["y"],
                    }
                ],
            }
        ],
    )
    llm.queue_response("diagnose", _make_diagnosis())
    llm.queue_response(
        "propose_plans",
        [{"name": "recover", "rationale": "apply schema", "steps": [_apply_schema_step()]}],
    )

    agent, context = build_agent(test_settings, llm)
    context.verifier.health_endpoint_ok = lambda: _async_true()  # type: ignore[method-assign]

    test_settings.db_file.unlink()
    await agent._recover_loop()

    event_types = [event.event_type for event in context.events.events]
    assert "agent.no_viable_candidates" in event_types
    assert "agent.recovered" in event_types


async def _async_true() -> bool:
    return True
