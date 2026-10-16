from __future__ import annotations

import pytest

from mi_agent.events.bus import EventBus
from mi_agent.supervisor.supervisor import Supervisor

pytestmark = pytest.mark.phase4


@pytest.mark.asyncio()
async def test_gate_attempt_increments_without_cap_field(test_settings) -> None:
    supervisor = Supervisor(test_settings, EventBus(test_settings.event_log_path))
    incident = await supervisor.open_incident("db_file_missing")

    gate1 = await supervisor.gate_attempt(incident)
    gate2 = await supervisor.gate_attempt(incident)
    gate3 = await supervisor.gate_attempt(incident)

    assert gate1.attempts == 1
    assert gate2.attempts == 2
    assert gate3.attempts == 3
    assert hasattr(gate1, "cap_reached") is False


@pytest.mark.asyncio()
async def test_attempts_continue_across_many_gates(test_settings) -> None:
    supervisor = Supervisor(test_settings, EventBus(test_settings.event_log_path))
    incident = await supervisor.open_incident("db_file_missing")

    last = None
    for _ in range(8):
        last = await supervisor.gate_attempt(incident)

    assert last is not None
    assert last.attempts == 8
