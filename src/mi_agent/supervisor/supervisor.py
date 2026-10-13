from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from shutil import copy2
from uuid import uuid4

from mi_agent.config import MISettings
from mi_agent.errors import IncidentLockError, ToolParameterValidationError
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
        self._attempt_timestamps: list[datetime] = []

    @property
    def lock(self) -> IncidentLock | None:
        return self._lock

    async def open_incident(self, failure_type: str) -> IncidentLock:
        if self._lock is not None:
            raise IncidentLockError("Another incident is active")
        now = self._utc_now()
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
        now = self._utc_now()
        elapsed = (now - incident.started_at).total_seconds()
        self._prune_hourly_attempts(now)

        if (
            self._settings.max_attempts_per_hour > 0
            and len(self._attempt_timestamps) >= self._settings.max_attempts_per_hour
        ):
            return AttemptGate(True, incident.attempts, elapsed, 0.0)

        if (
            self._settings.max_attempts_per_incident > 0
            and incident.attempts >= self._settings.max_attempts_per_incident
        ):
            return AttemptGate(True, incident.attempts, elapsed, 0.0)
        if (
            self._settings.max_incident_seconds > 0
            and elapsed >= self._settings.max_incident_seconds
        ):
            return AttemptGate(True, incident.attempts, elapsed, 0.0)

        backoff = 0.0
        if incident.attempts > 0:
            backoff = min(
                self._settings.supervisor_backoff_max_seconds,
                self._settings.supervisor_backoff_initial_seconds * (2 ** (incident.attempts - 1)),
            )
            await asyncio.sleep(backoff)
        incident.attempts += 1
        now_after = self._utc_now()
        self._attempt_timestamps.append(now_after)
        elapsed_after = (now_after - incident.started_at).total_seconds()
        await self._event_bus.publish(
            Event(
                event_type="supervisor.attempt_gated",
                payload={
                    "incident_id": incident.incident_id,
                    "attempt": incident.attempts,
                    "elapsed_seconds": elapsed_after,
                    "backoff_seconds": backoff,
                    "hourly_attempts": len(self._attempt_timestamps),
                    "hourly_budget": self._settings.max_attempts_per_hour,
                },
            )
        )
        return AttemptGate(False, incident.attempts, elapsed_after, backoff)

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(tz=UTC)

    def _prune_hourly_attempts(self, now: datetime) -> None:
        cutoff = now - timedelta(hours=1)
        self._attempt_timestamps = [stamp for stamp in self._attempt_timestamps if stamp >= cutoff]

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

    async def request_backup_restore(self, backup_id: str) -> dict[str, object]:
        if backup_id.strip() == "":
            raise ToolParameterValidationError("backup_id cannot be empty")

        backup_path = self._settings.backups_root / f"{backup_id}.db"
        if not backup_path.exists():
            raise ToolParameterValidationError(f"Backup id not found: {backup_id}")
        if not self._is_valid_sqlite_backup(backup_path):
            raise ToolParameterValidationError(f"Backup id failed verification: {backup_id}")

        db_path = self._settings.db_file
        quarantine_path: Path | None = None
        if db_path.exists():
            quarantine_dir = self._settings.backups_root / "quarantine"
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            quarantine_path = quarantine_dir / f"{backup_id}-{uuid4().hex}.db"
            copy2(db_path, quarantine_path)

        db_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_restore = db_path.with_name(f".{db_path.name}.restore_tmp")
        copy2(backup_path, tmp_restore)
        tmp_restore.replace(db_path)

        payload = {
            "backup_id": backup_id,
            "restored": True,
            "verified": True,
            "quarantined_previous": quarantine_path is not None,
            "budget_charged": 1,
        }
        await self._event_bus.publish(
            Event(event_type="supervisor.backup_restore_completed", payload=payload)
        )
        return payload

    @staticmethod
    def _is_valid_sqlite_backup(path: Path) -> bool:
        if path.stat().st_size == 0:
            return False
        try:
            with sqlite3.connect(path) as conn:
                row = conn.execute("PRAGMA integrity_check;").fetchone()
            return row is not None and str(row[0]).strip().lower() == "ok"
        except sqlite3.Error:
            return False
