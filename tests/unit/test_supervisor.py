from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from mi_agent.errors import IncidentLockError
from mi_agent.events.bus import EventBus
from mi_agent.supervisor.last_resort import LastResortError, LocalLastResortClient
from mi_agent.supervisor.supervisor import Supervisor


@pytest.mark.asyncio()
async def test_supervisor_backoff_growth_and_ceiling(
    test_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("mi_agent.supervisor.supervisor.asyncio.sleep", fake_sleep)

    restorer = LocalLastResortClient(test_settings.backups_root, test_settings.db_file)
    supervisor = Supervisor(test_settings, EventBus(), restorer)
    incident = await supervisor.open_incident("db_file_missing")

    first = await supervisor.gate_attempt(incident)
    second = await supervisor.gate_attempt(incident)
    third = await supervisor.gate_attempt(incident)

    assert not first.cap_reached
    assert second.backoff_seconds == pytest.approx(test_settings.supervisor_backoff_initial_seconds)
    assert third.backoff_seconds <= test_settings.supervisor_backoff_max_seconds
    assert sleeps[0] == pytest.approx(test_settings.supervisor_backoff_initial_seconds)


@pytest.mark.asyncio()
async def test_supervisor_cap_enforced_by_attempt_and_time(test_settings) -> None:
    restorer = LocalLastResortClient(test_settings.backups_root, test_settings.db_file)
    supervisor = Supervisor(test_settings, EventBus(), restorer)
    incident = await supervisor.open_incident("db_file_missing")

    incident.attempts = test_settings.max_attempts_per_incident
    gate = await supervisor.gate_attempt(incident)
    assert gate.cap_reached

    await supervisor.release_incident(incident.incident_id)
    incident2 = await supervisor.open_incident("db_file_missing")
    incident2.started_at = datetime.now(tz=UTC) - timedelta(
        seconds=test_settings.max_incident_seconds + 1
    )
    gate2 = await supervisor.gate_attempt(incident2)
    assert gate2.cap_reached


@pytest.mark.asyncio()
async def test_lock_contention(test_settings) -> None:
    supervisor = Supervisor(
        test_settings,
        EventBus(),
        LocalLastResortClient(test_settings.backups_root, test_settings.db_file),
    )
    await supervisor.open_incident("db_file_missing")
    with pytest.raises(IncidentLockError):
        await supervisor.open_incident("db_file_missing")


@pytest.mark.asyncio()
async def test_last_resort_missing_or_corrupt_backups(test_settings) -> None:
    restorer = LocalLastResortClient(test_settings.backups_root, test_settings.db_file)

    with pytest.raises(LastResortError):
        await restorer.restore_latest()

    test_settings.backups_root.mkdir(parents=True, exist_ok=True)
    bad = test_settings.backups_root / "bad.db"
    bad.write_bytes(b"")

    with pytest.raises(LastResortError):
        await restorer.restore_latest()


@pytest.mark.asyncio()
async def test_last_resort_restores_latest_snapshot(test_settings) -> None:
    backups = test_settings.backups_root
    backups.mkdir(parents=True, exist_ok=True)
    first = backups / "one.db"
    second = backups / "two.db"

    for candidate, value in ((first, "a"), (second, "b")):
        with sqlite3.connect(candidate) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS widgets(id INTEGER PRIMARY KEY, name TEXT)")
            conn.execute("INSERT INTO widgets(name) VALUES (?)", (value,))
            conn.commit()

    restorer = LocalLastResortClient(backups, test_settings.db_file)
    restored = await restorer.restore_latest()
    assert restored.endswith("two.db")
