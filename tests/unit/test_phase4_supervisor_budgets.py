from __future__ import annotations

import pytest

from mi_agent.events.bus import EventBus
from mi_agent.supervisor.last_resort import LocalLastResortClient
from mi_agent.supervisor.supervisor import Supervisor

pytestmark = pytest.mark.phase4


@pytest.mark.asyncio()
async def test_supervisor_enforces_hourly_budget_within_single_incident(test_settings) -> None:
    settings = test_settings.model_copy(
        update={
            "max_attempts_per_incident": 10,
            "max_attempts_per_hour": 2,
        }
    )
    supervisor = Supervisor(
        settings,
        EventBus(),
        LocalLastResortClient(settings.backups_root, settings.db_file),
    )
    incident = await supervisor.open_incident("db_file_missing")

    gate1 = await supervisor.gate_attempt(incident)
    gate2 = await supervisor.gate_attempt(incident)
    gate3 = await supervisor.gate_attempt(incident)

    assert gate1.cap_reached is False
    assert gate2.cap_reached is False
    assert gate3.cap_reached is True


@pytest.mark.asyncio()
async def test_supervisor_enforces_hourly_budget_across_incidents(test_settings) -> None:
    settings = test_settings.model_copy(
        update={
            "max_attempts_per_incident": 10,
            "max_attempts_per_hour": 2,
        }
    )
    supervisor = Supervisor(
        settings,
        EventBus(),
        LocalLastResortClient(settings.backups_root, settings.db_file),
    )

    incident1 = await supervisor.open_incident("db_file_missing")
    gate1 = await supervisor.gate_attempt(incident1)
    await supervisor.release_incident(incident1.incident_id)

    incident2 = await supervisor.open_incident("db_file_missing")
    gate2 = await supervisor.gate_attempt(incident2)
    await supervisor.release_incident(incident2.incident_id)

    incident3 = await supervisor.open_incident("db_file_missing")
    gate3 = await supervisor.gate_attempt(incident3)

    assert gate1.cap_reached is False
    assert gate2.cap_reached is False
    assert gate3.cap_reached is True

