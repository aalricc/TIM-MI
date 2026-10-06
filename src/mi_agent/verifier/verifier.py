from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import httpx

from mi_agent.config import MISettings
from mi_agent.events.bus import EventBus
from mi_agent.events.models import Event
from mi_agent.failure_types import normalize_failure_type
from mi_agent.workflow.models import VerificationResult


class Verifier:
    def __init__(self, settings: MISettings, event_bus: EventBus) -> None:
        self._settings = settings
        self._event_bus = event_bus

    async def verify(self, *, failure_type: str | None = None) -> VerificationResult:
        db_ping = await self.ping_db()
        integrity_ok = await self.integrity_check()
        schema_ok = await self.check_schema()
        rw_ok = await self.test_read_write()
        health_ok = await self.health_endpoint_ok()
        module_ok = self._inferred_failure_check(
            failure_type=failure_type,
            db_ping=db_ping,
            schema_ok=schema_ok,
        )
        healthy = all([db_ping, module_ok, integrity_ok, schema_ok, rw_ok, health_ok])
        result = VerificationResult(
            healthy=healthy,
            failure_type=None if healthy else failure_type,
            details={
                "db_ping": db_ping,
                "module_ok": module_ok,
                "integrity_ok": integrity_ok,
                "schema_ok": schema_ok,
                "rw_ok": rw_ok,
                "health_ok": health_ok,
            },
        )
        await self._event_bus.publish(
            Event(event_type="verifier.result", payload=result.model_dump(mode="json"))
        )
        return result

    @staticmethod
    def _inferred_failure_check(
        *,
        failure_type: str | None,
        db_ping: bool,
        schema_ok: bool,
    ) -> bool:
        canonical_failure_type = (
            normalize_failure_type(failure_type) if failure_type is not None else None
        )
        if canonical_failure_type == "db_file_missing":
            return db_ping and schema_ok
        return db_ping

    async def ping_db(self) -> bool:
        db_path = self._settings.db_file

        def _sync_ping() -> bool:
            if not db_path.exists():
                return False
            with sqlite3.connect(db_path) as conn:
                conn.execute("SELECT 1")
            return True

        try:
            return await asyncio.to_thread(_sync_ping)
        except sqlite3.Error:
            return False

    async def check_schema(self) -> bool:
        db_path = self._settings.db_file
        schema_path = self._settings.schema_file
        if not db_path.exists() or not schema_path.exists():
            return False
        expected = self._extract_tables(schema_path)

        def _sync_fetch() -> set[str]:
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            return {str(row[0]) for row in rows}

        try:
            actual = await asyncio.to_thread(_sync_fetch)
        except sqlite3.Error:
            return False
        return expected.issubset(actual)

    async def integrity_check(self) -> bool:
        db_path = self._settings.db_file
        if not db_path.exists():
            return False

        def _sync_integrity() -> bool:
            with sqlite3.connect(db_path) as conn:
                result = conn.execute("PRAGMA integrity_check;").fetchone()
            return result is not None and str(result[0]).lower() == "ok"

        try:
            return await asyncio.to_thread(_sync_integrity)
        except sqlite3.Error:
            return False

    async def test_read_write(self) -> bool:
        db_path = self._settings.db_file
        if not db_path.exists():
            return False

        def _sync_rw() -> bool:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS __mi_probe (id INTEGER PRIMARY KEY, v TEXT)"
                )
                conn.execute("INSERT INTO __mi_probe(v) VALUES ('ok')")
                row = conn.execute("SELECT v FROM __mi_probe ORDER BY id DESC LIMIT 1").fetchone()
                conn.execute("DELETE FROM __mi_probe")
                conn.commit()
            return row is not None and str(row[0]) == "ok"

        try:
            return await asyncio.to_thread(_sync_rw)
        except sqlite3.Error:
            return False

    async def health_endpoint_ok(self) -> bool:
        url = f"{self._settings.target_service_url}/health/deep"
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(url)
            if response.status_code != 200:
                return False
            payload = response.json()
            return bool(payload.get("ok", False))
        except (httpx.HTTPError, ValueError):
            return False

    @staticmethod
    def _extract_tables(schema_path: Path) -> set[str]:
        sql = schema_path.read_text(encoding="utf-8")
        tables: set[str] = set()
        for line in sql.splitlines():
            normalized = line.strip().lower()
            if normalized.startswith("create table"):
                parts = line.replace("(", " ").split()
                if len(parts) >= 3:
                    tables.add(parts[2].strip('"`'))
        return tables
