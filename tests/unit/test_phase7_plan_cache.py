from __future__ import annotations

import pytest

from mi_agent.config import MISettings
from mi_agent.llm.client import FakeLLMClient
from mi_agent.runtime import build_agent

pytestmark = [pytest.mark.phase7, pytest.mark.asyncio()]


def _write_missing_db_log(settings: MISettings) -> None:
    settings.app_log.write_text(
        "sqlite3.OperationalError: unable to open database file\n",
        encoding="utf-8",
    )


def _diagnosis(label: str = "db_file_missing") -> dict[str, object]:
    return {
        "failure_type": label,
        "hypotheses": ["database file missing"],
        "evidence": ["log signature matched"],
    }


def _apply_schema_plan() -> dict[str, object]:
    return {
        "name": "recover",
        "rationale": "recreate schema",
        "steps": [
            {
                "action_id": "apply_schema",
                "parameters": {"db_path": "data/app.db", "schema_path": "schema.sql"},
                "preconditions": ["probe:db_file.exists=false"],
                "expected_postconditions": ["probe:schema_ok=true"],
            }
        ],
    }


def _restart_service_plan() -> dict[str, object]:
    return {
        "name": "restart",
        "rationale": "restart after schema apply failed verification",
        "steps": [
            {
                "action_id": "restart_service",
                "parameters": {"service_name": "target-app"},
                "preconditions": ["probe:service.up=false"],
                "expected_postconditions": ["probe:service.up=true"],
            }
        ],
    }


async def _async_true() -> bool:
    return True


async def test_plan_cache_reuses_canonical_failure_type_and_keeps_verifier_gate(
    test_settings,
) -> None:
    llm = FakeLLMClient()
    llm.queue_response("diagnose", _diagnosis("database_initialization_storage_missing"))
    llm.queue_response("propose_plans", [_apply_schema_plan()])
    llm.queue_response("diagnose", _diagnosis("db_file_missing"))

    agent, context = build_agent(test_settings, llm)
    context.verifier.health_endpoint_ok = lambda: _async_true()  # type: ignore[method-assign]

    verify_calls = 0
    original_verify = context.verifier.verify

    async def _counted_verify(*, failure_type: str):
        nonlocal verify_calls
        verify_calls += 1
        return await original_verify(failure_type=failure_type)

    context.verifier.verify = _counted_verify  # type: ignore[method-assign]

    test_settings.db_file.unlink()
    _write_missing_db_log(test_settings)
    await agent._recover_loop()

    test_settings.db_file.unlink()
    _write_missing_db_log(test_settings)
    await agent._recover_loop()

    assert llm.calls["propose_plans"] == 1
    assert verify_calls >= 2
    events = [event.event_type for event in context.events.events]
    assert "agent.plan_cache_hit" in events


async def test_plan_cache_key_includes_state_fingerprint(test_settings) -> None:
    llm = FakeLLMClient()
    llm.queue_response("diagnose", _diagnosis())
    llm.queue_response("propose_plans", [_apply_schema_plan()])
    llm.queue_response("diagnose", _diagnosis())
    llm.queue_response("propose_plans", [_apply_schema_plan()])

    agent, context = build_agent(test_settings, llm)
    context.verifier.health_endpoint_ok = lambda: _async_true()  # type: ignore[method-assign]

    test_settings.db_file.unlink()
    _write_missing_db_log(test_settings)
    await agent._recover_loop()

    test_settings.db_file.unlink()
    test_settings.db_file.write_bytes(b"")
    _write_missing_db_log(test_settings)
    await agent._recover_loop()

    assert llm.calls["propose_plans"] == 2
    events = [event.event_type for event in context.events.events]
    assert events.count("agent.plan_cache_hit") == 0


async def test_exhausted_plan_cache_keeps_prior_actions_and_appends_new_ones(
    test_settings,
) -> None:
    llm = FakeLLMClient()
    llm.queue_response("diagnose", _diagnosis())
    llm.queue_response("propose_plans", [_apply_schema_plan()])
    llm.queue_response("diagnose", _diagnosis())
    llm.queue_response("propose_plans", [_restart_service_plan()])

    agent, context = build_agent(test_settings, llm)
    original_verify = context.verifier.verify
    verify_calls = 0

    async def _fail_once_then_pass(*, failure_type: str):
        nonlocal verify_calls
        verify_calls += 1
        result = await original_verify(failure_type=failure_type)
        if verify_calls == 1:
            return result.model_copy(update={"healthy": False})
        return result.model_copy(update={"healthy": True})

    context.verifier.verify = _fail_once_then_pass  # type: ignore[method-assign]

    test_settings.db_file.unlink()
    _write_missing_db_log(test_settings)
    await agent._recover_loop()

    cached_action_ids = [
        plan.steps[0].action_id
        for cached_plans in agent._plan_cache.values()
        for plan in cached_plans
    ]
    assert cached_action_ids == ["apply_schema", "restart_service"]
    assert llm.calls["propose_plans"] == 2
    events = [event.event_type for event in context.events.events]
    assert "agent.plan_cache_extended" in events
    assert "agent.plan_cache_hit" in events
