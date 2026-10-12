from __future__ import annotations

import pytest

from mi_agent.llm.client import FakeLLMClient
from mi_agent.runtime import build_agent

pytestmark = [pytest.mark.phase7, pytest.mark.asyncio()]


def _diagnosis() -> dict[str, object]:
    return {
        "failure_type": "db_file_missing",
        "hypotheses": ["database file missing"],
        "evidence": ["log signature matched"],
    }


def _apply_schema_plan() -> dict[str, object]:
    return {
        "name": "recover",
        "rationale": "apply schema",
        "steps": [
            {
                "action_id": "apply_schema",
                "parameters": {"db_path": "data/app.db", "schema_path": "schema.sql"},
                "preconditions": ["probe:db_file.exists=false"],
                "expected_postconditions": ["probe:schema_ok=true"],
            }
        ],
    }


def _distilled_skill() -> dict[str, object]:
    return {
        "name": "recover-db",
        "description": "Apply schema",
        "failure_type": "db_file_missing",
        "trigger_fingerprint": {},
        "steps": [
            {
                "action_id": "apply_schema",
                "parameters": {"db_path": "data/app.db", "schema_path": "schema.sql"},
            }
        ],
        "expected_postcondition": "healthy",
    }


async def _async_true() -> bool:
    return True


async def test_llm_usage_is_accounted_per_incident(test_settings) -> None:
    llm = FakeLLMClient()
    llm.queue_response("diagnose", _diagnosis())
    llm.queue_response("propose_plans", [_apply_schema_plan()])
    llm.queue_response("distill_skill", _distilled_skill())
    llm.queue_response("diagnose", _diagnosis())
    llm.queue_response("propose_plans", [_apply_schema_plan()])
    llm.queue_response("distill_skill", _distilled_skill())

    agent, context = build_agent(test_settings, llm)
    context.verifier.health_endpoint_ok = lambda: _async_true()  # type: ignore[method-assign]

    test_settings.db_file.unlink()
    test_settings.app_log.write_text(
        "sqlite3.OperationalError: unable to open database file\n",
        encoding="utf-8",
    )
    await agent._recover_loop()

    test_settings.db_file.unlink()
    test_settings.app_log.write_text(
        "sqlite3.OperationalError: unable to open database file\n",
        encoding="utf-8",
    )
    await agent._recover_loop()

    usage_events = [
        event for event in context.events.events if event.event_type == "agent.llm_usage_incident"
    ]
    assert len(usage_events) == 2

    first = usage_events[0].payload
    second = usage_events[1].payload
    assert first["llm_calls"] >= 2
    assert second["llm_calls"] >= 1
    assert first["llm_failures"] == 0
    assert second["llm_failures"] == 0
    assert first["estimated_cost_usd"] >= 0.0
    assert second["estimated_cost_usd"] >= 0.0
