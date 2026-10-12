from __future__ import annotations

import pytest

from mi_agent.llm.client import FakeLLMClient
from mi_agent.runtime import build_agent
from mi_agent.skills.model import SkillRecord, SkillStats, SkillStep

pytestmark = [pytest.mark.asyncio(), pytest.mark.phase4]


def _write_signature_log(settings) -> None:
    settings.app_log.write_text(
        "sqlite3.OperationalError: unable to open database file\n",
        encoding="utf-8",
    )


async def _async_true() -> bool:
    return True


async def test_degraded_mode_escalates_without_trusted_skill_when_llm_unavailable(
    test_settings,
) -> None:
    llm = FakeLLMClient()
    llm.queue_failure("diagnose", times=3)

    agent, context = build_agent(test_settings, llm)

    test_settings.db_file.unlink()
    _write_signature_log(test_settings)
    await agent._recover_loop()

    event_types = [event.event_type for event in context.events.events]
    assert "agent.degraded_mode" in event_types
    assert "operator.escalated" in event_types
    assert llm.calls["propose_plans"] == 0


async def test_degraded_mode_uses_trusted_skill_when_llm_unavailable(test_settings) -> None:
    llm = FakeLLMClient()
    llm.queue_failure("diagnose", times=3)

    agent, context = build_agent(test_settings, llm)
    context.verifier.health_endpoint_ok = lambda: _async_true()  # type: ignore[method-assign]

    context.skill_library.register(
        SkillRecord(
            name="recover-db-from-schema",
            description="Apply schema",
            failure_type="db_file_missing",
            trigger_fingerprint={},
            steps=[
                SkillStep(
                    primitive_id="apply_schema",
                    parameters={"db_path": "data/app.db", "schema_path": "schema.sql"},
                )
            ],
            expected_postcondition="healthy",
            stats=SkillStats(success_count=1),
        )
    )

    test_settings.db_file.unlink()
    _write_signature_log(test_settings)
    await agent._recover_loop()

    event_types = [event.event_type for event in context.events.events]
    assert "agent.degraded_mode" in event_types
    assert "agent.skill_executed" in event_types
    assert "operator.escalated" not in event_types
    assert test_settings.db_file.exists()
    assert llm.calls["propose_plans"] == 0
