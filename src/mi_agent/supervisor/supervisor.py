from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from mi_agent.config import MISettings
from mi_agent.errors import IncidentLockError
from mi_agent.events.bus import EventBus
from mi_agent.events.models import Event
from mi_agent.supervisor.last_resort import LastResortClient


@dataclass(slots=True)
class IncidentLock:
    incident_id: str
    failure_type: str
    started_at: datetime
    attempts: int = 0


@dataclass(frozen=True, slots=True)
class AttemptGate:
    cap_reached: bool
    attempts: int
    elapsed_seconds: float
    backoff_seconds: float


class Supervisor:
    def __init__(
        self,
        settings: MISettings,
        event_bus: EventBus,
        restorer: LastResortClient,
    ) -> None:
        self._settings = settings
        self._event_bus = event_bus
        self._restorer = restorer
        self._lock: IncidentLock | None = None

    @property
    def lock(self) -> IncidentLock | None:
        return self._lock

    async def open_incident(self, failure_type: str) -> IncidentLock:
        if self._lock is not None:
            raise IncidentLockError("Another incident is active")
        now = datetime.now(tz=UTC)
        incident = IncidentLock(
            incident_id=f"inc-{int(now.timestamp() * 1000)}",
            failure_type=failure_type,
            started_at=now,
        )
        self._lock = incident
        await self._event_bus.publish(
            Event(
                event_type="supervisor.incident_opened",
                payload={"incident_id": incident.incident_id},
            )
        )
        return incident

    async def release_incident(self, incident_id: str) -> None:
        if self._lock is None:
            return
        if self._lock.incident_id != incident_id:
            return
        await self._event_bus.publish(
            Event(event_type="supervisor.incident_released", payload={"incident_id": incident_id})
        )
        self._lock = None

    async def gate_attempt(self, incident: IncidentLock) -> AttemptGate:
        now = datetime.now(tz=UTC)
        elapsed = (now - incident.started_at).total_seconds()
        if incident.attempts >= self._settings.max_attempts_per_incident:
            return AttemptGate(True, incident.attempts, elapsed, 0.0)
        if elapsed >= self._settings.max_incident_seconds:
            return AttemptGate(True, incident.attempts, elapsed, 0.0)

        backoff = 0.0
        if incident.attempts > 0:
            backoff = min(
                self._settings.supervisor_backoff_max_seconds,
                self._settings.supervisor_backoff_initial_seconds * (2 ** (incident.attempts - 1)),
            )
            await asyncio.sleep(backoff)
        incident.attempts += 1
        now_after = datetime.now(tz=UTC)
        elapsed_after = (now_after - incident.started_at).total_seconds()
        await self._event_bus.publish(
            Event(
                event_type="supervisor.attempt_gated",
                payload={
                    "incident_id": incident.incident_id,
                    "attempt": incident.attempts,
                    "elapsed_seconds": elapsed_after,
                    "backoff_seconds": backoff,
                },
            )
        )
        return AttemptGate(False, incident.attempts, elapsed_after, backoff)

    async def run_last_resort(self, incident: IncidentLock) -> str:
        backup_path = await self._restorer.restore_latest()
        await self._event_bus.publish(
            Event(
                event_type="supervisor.last_resort_restored",
                payload={
                    "incident_id": incident.incident_id,
                    "backup_path": str(backup_path),
                },
            )
        )
        return str(backup_path)
