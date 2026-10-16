from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from shutil import copy2
from uuid import uuid4

from mi_agent.config import MISettings
from mi_agent.errors import IncidentLockError, ToolParameterValidationError
from mi_agent.events.bus import EventBus
from mi_agent.events.models import Event


@dataclass(slots=True)
class IncidentLock:
    incident_id: str
    failure_type: str
    started_at: datetime
    attempts: int = 0


@dataclass(frozen=True, slots=True)
class AttemptGate:
    attempts: int
    elapsed_seconds: float
    backoff_seconds: float


class Supervisor:
    def __init__(
        self,
        settings: MISettings,
        event_bus: EventBus,
    ) -> None:
        self._settings = settings
        self._event_bus = event_bus
        self._lock: IncidentLock | None = None

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
        backoff = 0.0
        if incident.attempts > 0:
            exponent = min(incident.attempts - 1, 16)
            backoff = min(
                self._settings.supervisor_backoff_max_seconds,
                self._settings.supervisor_backoff_initial_seconds * (2**exponent),
            )
            await asyncio.sleep(backoff)
        incident.attempts += 1
        now_after = self._utc_now()
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
        return AttemptGate(incident.attempts, elapsed_after, backoff)

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(tz=UTC)

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
