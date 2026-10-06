from __future__ import annotations

import sqlite3

import pytest

from mi_agent.config import MISettings
from mi_agent.llm.client import FakeLLMClient
from mi_agent.runtime import build_agent
from mi_agent.skills.model import SkillRecord, SkillStep


class FakeAlertSink:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def send(self, title: str, message: str) -> None:
        self.messages.append((title, message))


def _write_signature_log(settings: MISettings) -> None:
    settings.app_log.write_text(
        "sqlite3.OperationalError: unable to open database file\n",
        encoding="utf-8",
    )


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
    }


@pytest.mark.asyncio()
async def test_recovers_missing_db_and_creates_skill(test_settings) -> None:
    llm = FakeLLMClient()
    llm.queue_response("diagnose", _make_diagnosis())
    llm.queue_response(
        "propose_plans",
        [
            {
                "name": "recover-by-schema",
                "rationale": "schema can recreate db",
                "steps": [_apply_schema_step()],
            }
        ],
    )
    llm.queue_response(
        "distill_skill",
        {
            "name": "recover-db-from-schema",
            "description": "Apply schema to recreate DB",
            "failure_type": "db_file_missing",
            "trigger_fingerprint": {},
            "preconditions": ["schema exists"],
            "steps": [_apply_schema_step()],
            "expected_postcondition": "deep health check passes",
        },
    )

    agent, context = build_agent(test_settings, llm)
    context.verifier.health_endpoint_ok = lambda: _async_true()  # type: ignore[method-assign]

    test_settings.db_file.unlink()
    _write_signature_log(test_settings)

    await agent._recover_loop()

    assert test_settings.db_file.exists()
    skills = context.skill_library.all("db_file_missing")
    assert any(skill.name == "recover-db-from-schema" for skill in skills)


@pytest.mark.asyncio()
async def test_second_failure_uses_learned_skill_without_planning_call(test_settings) -> None:
    llm = FakeLLMClient()
    llm.queue_response("diagnose", _make_diagnosis())
    llm.queue_response(
        "propose_plans",
        [{"name": "recover", "rationale": "", "steps": [_apply_schema_step()]}],
    )
    llm.queue_response(
        "distill_skill",
        {
            "name": "recover-db-from-schema",
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
            "name": "recover-db-from-schema",
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
    _write_signature_log(test_settings)
    await agent._recover_loop()

    first_planning_calls = llm.calls["propose_plans"]
    assert first_planning_calls == 1

    test_settings.db_file.unlink()
    _write_signature_log(test_settings)
    await agent._recover_loop()

    assert llm.calls["propose_plans"] == first_planning_calls


@pytest.mark.asyncio()
async def test_canonicalized_failure_type_rechecks_skills_before_planning(test_settings) -> None:
    llm = FakeLLMClient()
    llm.queue_response(
        "diagnose",
        {
            "failure_type": "database_initialization_storage_missing",
            "hypotheses": ["database unavailable"],
            "evidence": ["probe indicates missing db"],
        },
    )
    llm.queue_response("match_skill", {"skill_name": "recover-db-from-schema", "confidence": 0.9})
    llm.queue_response(
        "distill_skill",
        {
            "name": "recover-db-from-schema",
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
            name="recover-db-from-schema",
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

    test_settings.db_file.unlink()
    _write_signature_log(test_settings)
    await agent._recover_loop()

    assert llm.calls["propose_plans"] == 0
    assert llm.calls["match_skill"] == 1
    events = [event.event_type for event in context.events.events]
    assert "agent.skill_executed" in events


@pytest.mark.asyncio()
async def test_llm_outage_still_allows_learned_skill_execution(test_settings) -> None:
    llm = FakeLLMClient()
    llm.queue_response("diagnose", _make_diagnosis())
    llm.queue_response(
        "propose_plans",
        [{"name": "recover", "rationale": "", "steps": [_apply_schema_step()]}],
    )
    llm.queue_response(
        "distill_skill",
        {
            "name": "recover-db-from-schema",
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
            "name": "recover-db-from-schema",
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
    _write_signature_log(test_settings)
    await agent._recover_loop()

    llm.queue_failure("diagnose", times=2)
    llm.queue_failure("propose_plans", times=2)

    test_settings.db_file.unlink()
    _write_signature_log(test_settings)
    await agent._recover_loop()

    assert test_settings.db_file.exists()


@pytest.mark.asyncio()
async def test_first_plan_fails_second_succeeds_and_values_reflect_outcome(test_settings) -> None:
    llm = FakeLLMClient()
    llm.queue_response("diagnose", _make_diagnosis())
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
    llm.queue_response("diagnose", _make_diagnosis())
    llm.queue_response(
        "propose_plans",
        [{"name": "good-plan", "rationale": "", "steps": [_apply_schema_step()]}],
    )
    llm.queue_response(
        "write_lesson",
        {"lesson": "Avoid restart_service when DB file is missing."},
    )
    llm.queue_response(
        "distill_skill",
        {
            "name": "recover-db-from-schema",
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
    _write_signature_log(test_settings)
    await agent._recover_loop()

    snapshot = context.policy.snapshot()
    values = snapshot.failures["db_file_missing"].actions
    restart_key = "restart_service|"
    assert any(key.startswith(restart_key) for key in values)
    assert any(key.startswith("apply_schema|") for key in values)


@pytest.mark.asyncio()
async def test_invalid_readonly_plan_does_not_use_fallback_candidates(test_settings) -> None:
    settings = test_settings.model_copy(update={"max_attempts_per_incident": 1})
    llm = FakeLLMClient()
    llm.queue_response("diagnose", _make_diagnosis())
    llm.queue_response(
        "propose_plans",
        [
            {
                "name": "readonly-plan",
                "rationale": "inspect only",
                "steps": [{"action_id": "read_app_file", "parameters": {}}],
            }
        ],
    )

    agent, context = build_agent(settings, llm)
    context.verifier.health_endpoint_ok = lambda: _async_true()  # type: ignore[method-assign]

    settings.db_file.unlink()
    _write_signature_log(settings)
    await agent._recover_loop()

    events = [event.event_type for event in context.events.events]
    assert "agent.no_viable_candidates" in events
    assert "agent.fallback_candidates_used" not in events


@pytest.mark.asyncio()
async def test_cap_reached_triggers_last_resort_restore_and_alert(test_settings) -> None:
    # reduce cap so test is quick
    settings = test_settings.model_copy(update={"max_attempts_per_incident": 2})

    backup_db = settings.backups_root / "backup-1.db"
    backup_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(backup_db) as conn:
        conn.executescript(settings.schema_file.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO widgets(name) VALUES ('from-backup')")
        conn.commit()

    llm = FakeLLMClient()
    llm.queue_response("diagnose", _make_diagnosis())
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
    llm.queue_response("diagnose", _make_diagnosis())
    llm.queue_response(
        "propose_plans",
        [
            {
                "name": "bad-plan-2",
                "rationale": "",
                "steps": [{"action_id": "restart_service", "parameters": {}}],
            }
        ],
    )
    llm.queue_response("write_lesson", {"lesson": "Fallback was required after repeated failures."})

    agent, context = build_agent(settings, llm)
    async def _always_false() -> bool:
        return False

    context.verifier.health_endpoint_ok = _always_false  # type: ignore[method-assign]
    fake_alerts = FakeAlertSink()
    context.alerts = fake_alerts

    settings.db_file.unlink()
    _write_signature_log(settings)
    await agent._recover_loop()

    assert settings.db_file.exists()
    assert fake_alerts.messages
    snapshot = context.policy.snapshot()
    assert "db_file_missing" in snapshot.failures


@pytest.mark.asyncio()
async def test_agent_cannot_self_report_success_only_verifier_can_resolve(test_settings) -> None:
    llm = FakeLLMClient()
    llm.queue_response("diagnose", _make_diagnosis())
    llm.queue_response(
        "propose_plans",
        [{"name": "recover", "rationale": "", "steps": [_apply_schema_step()]}],
    )

    agent, context = build_agent(test_settings, llm)

    async def always_false() -> bool:
        return False

    context.verifier.health_endpoint_ok = always_false  # type: ignore[method-assign]

    backup = test_settings.backups_root / "backup.db"
    backup.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(backup) as conn:
        conn.executescript(test_settings.schema_file.read_text(encoding="utf-8"))
        conn.commit()

    test_settings.db_file.unlink()
    _write_signature_log(test_settings)
    await agent._recover_loop()

    events = [event.event_type for event in context.events.events]
    assert "agent.incident_unresolved" in events


@pytest.mark.asyncio()
async def test_distill_failure_emits_event(test_settings) -> None:
    llm = FakeLLMClient()
    llm.queue_response("diagnose", _make_diagnosis())
    llm.queue_response(
        "propose_plans",
        [{"name": "recover", "rationale": "", "steps": [_apply_schema_step()]}],
    )
    llm.queue_response(
        "distill_skill",
        {
            "name": "bad-distilled-skill",
            "description": "invalid because not in successful episode",
            "failure_type": "db_file_missing",
            "trigger_fingerprint": {},
            "steps": [{"action_id": "restart_service", "parameters": {}}],
            "expected_postcondition": "healthy",
        },
    )

    agent, context = build_agent(test_settings, llm)
    context.verifier.health_endpoint_ok = lambda: _async_true()  # type: ignore[method-assign]

    test_settings.db_file.unlink()
    _write_signature_log(test_settings)
    await agent._recover_loop()

    events = [event.event_type for event in context.events.events]
    assert "skills.distill_failed" in events


@pytest.mark.asyncio()
async def test_failed_action_not_reselected_when_state_unchanged(test_settings) -> None:
    settings = test_settings.model_copy(update={"epsilon_start": 0.0, "epsilon_min": 0.0})

    llm = FakeLLMClient()
    llm.queue_response("diagnose", _make_diagnosis())
    llm.queue_response(
        "propose_plans",
        [
            {
                "name": "first",
                "rationale": "",
                "steps": [
                    {
                        "action_id": "create_directory",
                        "parameters": {"path": "app/data"},
                    }
                ],
            }
        ],
    )
    llm.queue_response("diagnose", _make_diagnosis())
    llm.queue_response(
        "propose_plans",
        [
            {
                "name": "repeat",
                "rationale": "",
                "steps": [
                    {
                        "action_id": "create_directory",
                        "parameters": {"path": "app/data"},
                    }
                ],
            }
        ],
    )
    agent, context = build_agent(settings, llm)
    context.verifier.health_endpoint_ok = lambda: _async_true()  # type: ignore[method-assign]

    settings.db_file.unlink()
    _write_signature_log(settings)
    await agent._recover_loop()

    action_failed_events = [
        event
        for event in context.events.events
        if event.event_type == "agent.action_failed"
    ]
    assert all(
        str(event.payload.get("error", ""))
        != "Action already failed with same parameters and unchanged system state"
        for event in action_failed_events
    )
    assert "agent.no_viable_candidates" in [event.event_type for event in context.events.events]


async def _async_true() -> bool:
    return True
