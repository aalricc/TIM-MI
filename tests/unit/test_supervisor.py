from __future__ import annotations

import sqlite3

import pytest

from mi_agent.errors import IncidentLockError, ToolParameterValidationError
from mi_agent.events.bus import EventBus
from mi_agent.supervisor.supervisor import Supervisor

pytestmark = pytest.mark.phase4


@pytest.mark.asyncio()
async def test_supervisor_backoff_growth_and_ceiling(test_settings) -> None:
    event_bus = EventBus(test_settings.event_log_path)
    supervisor = Supervisor(test_settings, event_bus)
    incident = await supervisor.open_incident("db_file_missing")

    first = await supervisor.gate_attempt(incident)
    second = await supervisor.gate_attempt(incident)
    third = await supervisor.gate_attempt(incident)

    assert first.attempts == 1
    assert first.backoff_seconds == 0.0
    assert second.backoff_seconds <= test_settings.supervisor_backoff_max_seconds
    assert third.backoff_seconds <= test_settings.supervisor_backoff_max_seconds


@pytest.mark.asyncio()
async def test_supervisor_lock_contention(test_settings) -> None:
    event_bus = EventBus(test_settings.event_log_path)
    supervisor = Supervisor(test_settings, event_bus)
    incident = await supervisor.open_incident("db_file_missing")

    with pytest.raises(IncidentLockError):
        await supervisor.open_incident("db_missing")

    await supervisor.release_incident(incident.incident_id)
    again = await supervisor.open_incident("db_missing")
    assert again.failure_type == "db_missing"


@pytest.mark.asyncio()
async def test_request_backup_restore_missing_backup_raises(test_settings) -> None:
    event_bus = EventBus(test_settings.event_log_path)
    supervisor = Supervisor(test_settings, event_bus)

    with pytest.raises(ToolParameterValidationError):
        await supervisor.request_backup_restore("missing")


@pytest.mark.asyncio()
async def test_request_backup_restore_restores_valid_snapshot(test_settings) -> None:
    event_bus = EventBus(test_settings.event_log_path)
    supervisor = Supervisor(test_settings, event_bus)

    backup_id = "backup_ok"
    backup_path = test_settings.backups_root / f"{backup_id}.db"
    backup_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(backup_path) as conn:
        conn.executescript("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT NOT NULL);")
        conn.execute("INSERT INTO widgets(name) VALUES ('restored')")
        conn.commit()

    result = await supervisor.request_backup_restore(backup_id)
    assert result["restored"] is True

    with sqlite3.connect(test_settings.db_file) as conn:
        value = conn.execute("SELECT name FROM widgets ORDER BY id DESC LIMIT 1").fetchone()
    assert value is not None


@pytest.mark.asyncio()
async def test_release_incident_is_idempotent(test_settings) -> None:
    event_bus = EventBus(test_settings.event_log_path)
    supervisor = Supervisor(test_settings, event_bus)

    incident = await supervisor.open_incident("db_file_missing")
    await supervisor.release_incident("different")
    assert supervisor.lock is not None

    await supervisor.release_incident(incident.incident_id)
    assert supervisor.lock is None

    await supervisor.release_incident(incident.incident_id)
    assert supervisor.lock is None
