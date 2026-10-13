from __future__ import annotations

import pytest

from mi_agent.llm.client import FakeLLMClient
from mi_agent.runtime import build_agent

pytestmark = [pytest.mark.asyncio(), pytest.mark.phase4]


def _write_signature_log(settings) -> None:
    settings.app_log.write_text(
        "sqlite3.OperationalError: unable to open database file\n",
        encoding="utf-8",
    )


async def test_llm_unavailable_ends_unresolved_autonomous(test_settings) -> None:
    llm = FakeLLMClient()
    llm.queue_failure("diagnose", times=3)

    agent, context = build_agent(test_settings, llm)

    test_settings.db_file.unlink()
    _write_signature_log(test_settings)
    await agent._recover_loop()

    event_types = [event.event_type for event in context.events.events]
    assert "agent.unresolved_autonomous" in event_types
    assert "agent.degraded_mode" not in event_types
    assert llm.calls["propose_plans"] == 0
