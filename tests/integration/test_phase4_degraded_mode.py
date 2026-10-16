from __future__ import annotations

import asyncio

import pytest

from mi_agent.llm.client import FakeLLMClient
from mi_agent.runtime import build_agent

pytestmark = [pytest.mark.asyncio(), pytest.mark.phase4]


def _write_signature_log(settings) -> None:
    settings.app_log.write_text(
        "sqlite3.OperationalError: unable to open database file\n",
        encoding="utf-8",
    )


async def test_llm_unavailable_retries_until_cancelled(test_settings) -> None:
    llm = FakeLLMClient()
    llm.queue_failure("diagnose", times=3)

    agent, context = build_agent(test_settings, llm)

    test_settings.db_file.unlink()
    _write_signature_log(test_settings)
    task = asyncio.create_task(agent._recover_loop())
    for _ in range(200):
        if any(event.event_type == "llm.diagnose_retry" for event in context.events.events):
            break
        await asyncio.sleep(0.01)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    event_types = [event.event_type for event in context.events.events]
    assert "llm.diagnose_retry" in event_types
    assert "agent.unresolved_autonomous" not in event_types
    assert llm.calls["propose_plans"] == 0
