from __future__ import annotations

from pathlib import Path

import pytest

from mi_agent.llm.client import FakeLLMClient
from mi_agent.runtime import build_agent
from mi_agent.skills.library import SkillLibrary
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


def _skill(name: str = "recover-db") -> SkillRecord:
    return SkillRecord(
        name=name,
        description="Apply schema",
        failure_type="db_file_missing",
        trigger_fingerprint={"probes": "missing"},
        steps=[
            SkillStep(
                primitive_id="apply_schema",
                parameters={"db_path": "data/app.db", "schema_path": "schema.sql"},
            )
        ],
        expected_postcondition="healthy",
    )


async def test_cold_start_begins_with_no_skills(test_settings) -> None:
    _, context = build_agent(test_settings, FakeLLMClient())

    assert context.skill_library.all(include_inactive=True) == []


async def test_repeat_incident_reuses_skill_with_fewer_llm_calls_and_actions(test_settings) -> None:
    llm = FakeLLMClient()
    llm.queue_response("diagnose", _diagnosis())
    llm.queue_response(
        "propose_plans",
        [
            {
                "name": "bad-plan",
                "rationale": "",
                "steps": [{"action_id": "restart_service", "parameters": {}}],
            }
        ],
    )
    llm.queue_response("diagnose", _diagnosis())
    llm.queue_response(
        "propose_plans",
        [{"name": "good-plan", "rationale": "", "steps": [_apply_schema_step()]}],
    )
    llm.queue_response("write_lesson", {"lesson": "Restart did not recover missing DB"})
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

    test_settings.db_file.unlink()
    _write_missing_db_log(test_settings)
    await agent._recover_loop()

    first_llm_calls = sum(llm.calls.values())
    first_actions = len(context.decision_log.read_all())

    test_settings.db_file.unlink()
    _write_missing_db_log(test_settings)
    await agent._recover_loop()

    total_llm_calls = sum(llm.calls.values())
    second_llm_calls = total_llm_calls - first_llm_calls

    total_actions = len(context.decision_log.read_all())
    second_actions = total_actions - first_actions

    assert second_llm_calls < first_llm_calls
    assert second_actions < first_actions


async def test_failed_preconditions_fall_through_to_planning_path(test_settings) -> None:
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

    events = [event.event_type for event in context.events.events]
    assert "agent.skill_precondition_failed" in events
    assert llm.calls["propose_plans"] == 1


async def test_regressed_skill_is_demoted_to_deprecated(tmp_path: Path) -> None:
    library = SkillLibrary(
        tmp_path / "skills.json",
        candidate_to_canary_successes=1,
        canary_to_trusted_successes=1,
        trusted_regression_failures=2,
    )
    library.register(_skill())
    library.record_result("recover-db", success=True, recovery_seconds=1.0)
    library.record_result("recover-db", success=True, recovery_seconds=1.0)

    library.record_result("recover-db", success=False, recovery_seconds=1.0)
    library.record_result("recover-db", success=False, recovery_seconds=1.0)

    demoted = library.get("recover-db")
    assert demoted is not None
    assert demoted.lifecycle_state == "deprecated"
    assert demoted.active is False
