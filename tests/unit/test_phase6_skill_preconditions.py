from __future__ import annotations

import pytest

from mi_agent.llm.client import FakeLLMClient
from mi_agent.runtime import build_agent
from mi_agent.skills.model import SkillRecord, SkillStep

pytestmark = [pytest.mark.phase6, pytest.mark.asyncio()]


def _diagnosis() -> dict[str, object]:
    return {
        "failure_type": "db_file_missing",
        "hypotheses": ["database file missing"],
        "evidence": ["log signature matched"],
    }


def _apply_schema_step() -> dict[str, object]:
    return {
        "action_id": "apply_schema",
        "parameters": {"db_path": "data/app.db", "schema_path": "schema.sql"},
    }


async def _async_true() -> bool:
    return True


def _write_missing_db_log(test_settings) -> None:
    test_settings.app_log.write_text(
        "sqlite3.OperationalError: unable to open database file\n",
        encoding="utf-8",
    )


async def test_skill_precondition_failure_falls_through_to_normal_planning(test_settings) -> None:
    llm = FakeLLMClient()
    llm.queue_response("diagnose", _diagnosis())
    llm.queue_response("match_skill", {"skill_name": "recover-db", "confidence": 0.9})
    llm.queue_response("diagnose", _diagnosis())
    llm.queue_response(
        "propose_plans",
        [{"name": "recover", "rationale": "", "steps": [_apply_schema_step()]}],
    )
    llm.queue_response(
        "distill_skill",
        {
            "name": "recover-db",
            "description": "Apply schema",
            "failure_type": "db_file_missing",
            "trigger_fingerprint": {},
            "steps": [_apply_schema_step()],
            "expected_postcondition": "healthy",
        },
    )

    agent, context = build_agent(test_settings, llm)
    context.verifier.health_endpoint_ok = lambda: _async_true()  # type: ignore[method-assign]
    context.skill_library.register(
        SkillRecord(
            name="recover-db",
            description="Apply schema",
            failure_type="db_file_missing",
            trigger_fingerprint={"probes": "different", "logs": "different"},
            preconditions=["probe:db_file.exists=true"],
            steps=[
                SkillStep(
                    primitive_id="apply_schema",
                    parameters={"db_path": "data/app.db", "schema_path": "schema.sql"},
                )
            ],
            expected_postcondition="healthy",
        )
    )

    test_settings.db_file.unlink()
    _write_missing_db_log(test_settings)
    await agent._recover_loop()

    assert test_settings.db_file.exists()
    assert llm.calls["propose_plans"] == 1
    events = [event.event_type for event in context.events.events]
    assert "agent.skill_precondition_failed" in events
    assert "agent.skill_executed" not in events


async def test_skill_execution_rechecks_read_only_probes_before_running(test_settings) -> None:
    llm = FakeLLMClient()
    llm.queue_response("diagnose", _diagnosis())
    llm.queue_response("match_skill", {"skill_name": "recover-db", "confidence": 0.9})
    llm.queue_response(
        "distill_skill",
        {
            "name": "recover-db",
            "description": "Apply schema",
            "failure_type": "db_file_missing",
            "trigger_fingerprint": {},
            "steps": [_apply_schema_step()],
            "expected_postcondition": "healthy",
        },
    )

    agent, context = build_agent(test_settings, llm)
    context.verifier.health_endpoint_ok = lambda: _async_true()  # type: ignore[method-assign]
    context.skill_library.register(
        SkillRecord(
            name="recover-db",
            description="Apply schema",
            failure_type="db_file_missing",
            trigger_fingerprint={"probes": "different", "logs": "different"},
            steps=[
                SkillStep(
                    primitive_id="apply_schema",
                    parameters={"db_path": "data/app.db", "schema_path": "schema.sql"},
                )
            ],
            expected_postcondition="healthy",
        )
    )

    probes_calls = 0
    original_collect = agent._diagnoser.collect_probes

    async def _counted_collect_probes() -> dict[str, object]:
        nonlocal probes_calls
        probes_calls += 1
        return await original_collect()

    agent._diagnoser.collect_probes = _counted_collect_probes  # type: ignore[method-assign]

    test_settings.db_file.unlink()
    _write_missing_db_log(test_settings)
    await agent._recover_loop()

    assert test_settings.db_file.exists()
    assert probes_calls >= 2
    assert llm.calls["propose_plans"] == 0
    events = [event.event_type for event in context.events.events]
    assert "agent.skill_executed" in events
