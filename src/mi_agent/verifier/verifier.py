from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from pathlib import Path

import httpx

from mi_agent.config import MISettings
from mi_agent.events.bus import EventBus
from mi_agent.events.models import Event
from mi_agent.workflow.models import VerificationResult


class Verifier:
    VERSION = "v2"

    def __init__(self, settings: MISettings, event_bus: EventBus) -> None:
        self._settings = settings
        self._event_bus = event_bus
        self._ledger_path = settings.state_dir / "ack_ledger.json"

    async def verify(self, *, failure_type: str | None = None) -> VerificationResult:
        del failure_type
        db_ping = await self.ping_db()
        integrity_ok = await self.integrity_check()
        schema_ok = await self.check_schema()
        rw_ok = await self.test_read_write()
        health_ok = await self.health_endpoint_ok()
        journal_clear = await self.check_no_hot_journal()
        volume_clear = await self.check_no_volume_fill_marker()
        runtime_config_ok = await self.runtime_config_ok()
        continuity = await self.data_continuity_check()

        base_healthy = all(
            [
                db_ping,
                integrity_ok,
                schema_ok,
                rw_ok,
                health_ok,
                journal_clear,
                volume_clear,
                runtime_config_ok,
                continuity["ok"],
            ]
        )
        delayed_ok = True
        if base_healthy:
            delay = self._settings.verifier_delayed_reverify_seconds
            if delay > 0:
                await asyncio.sleep(delay)
            delayed_ping = await self.ping_db()
            delayed_integrity = await self.integrity_check()
            delayed_health = await self.health_endpoint_ok()
            delayed_ok = delayed_ping and delayed_integrity and delayed_health

        healthy = base_healthy and delayed_ok
        result = VerificationResult(
            healthy=healthy,
            failure_type=None,
            details={
                "verifier_version": self.VERSION,
                "db_ping": db_ping,
                "integrity_ok": integrity_ok,
                "schema_ok": schema_ok,
                "rw_ok": rw_ok,
                "health_ok": health_ok,
                "journal_clear": journal_clear,
                "volume_clear": volume_clear,
                "runtime_config_ok": runtime_config_ok,
                "data_continuity": continuity,
                "delayed_reverify_ok": delayed_ok,
            },
        )
        await self._event_bus.publish(
            Event(event_type="verifier.result", payload=result.model_dump(mode="json"))
        )
        return result

    async def data_continuity_check(self) -> dict[str, int | bool]:
        entries = self._load_ledger_entries()
        if not entries:
            return {"ok": True, "checked": 0, "missing": 0, "known": False}

        db_path = self._settings.db_file
        if not db_path.exists():
            return {"ok": False, "checked": len(entries), "missing": len(entries), "known": True}

        def _sync_check() -> tuple[int, int]:
            checked = 0
            missing = 0
            with sqlite3.connect(db_path) as conn:
                for entry in entries:
                    checked += 1
                    table = entry["table"]
                    pk_column = entry["pk_column"]
                    pk_value = entry["pk_value"]
                    query = (
                        f'SELECT 1 FROM "{table}" WHERE "{pk_column}" = ? LIMIT 1'
                    )
                    row = conn.execute(query, (pk_value,)).fetchone()
                    if row is None:
                        missing += 1
            return checked, missing

        try:
            checked, missing = await asyncio.to_thread(_sync_check)
        except sqlite3.Error:
            return {"ok": False, "checked": len(entries), "missing": len(entries), "known": True}
        return {"ok": missing == 0, "checked": checked, "missing": missing, "known": True}

    def _load_ledger_entries(self) -> list[dict[str, int | str]]:
        if not self._ledger_path.exists():
            return []
        try:
            raw = json.loads(self._ledger_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        if not isinstance(raw, list):
            return []

        valid: list[dict[str, int | str]] = []
        identifier = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
        for item in raw:
            if not isinstance(item, dict):
                continue
            table = item.get("table")
            pk_column = item.get("pk_column")
            pk_value = item.get("pk_value")
            if not isinstance(table, str) or not identifier.fullmatch(table):
                continue
            if not isinstance(pk_column, str) or not identifier.fullmatch(pk_column):
                continue
            if not isinstance(pk_value, (int, str)):
                continue
            valid.append(
                {"table": table, "pk_column": pk_column, "pk_value": pk_value}
            )
        return valid

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

    async def check_no_hot_journal(self) -> bool:
        journal_path = self._settings.db_file.with_name(f"{self._settings.db_file.name}-journal")
        lock_path = self._settings.db_file.with_name(f"{self._settings.db_file.name}.lock")
        return (not journal_path.exists()) and (not lock_path.exists())

    async def check_no_volume_fill_marker(self) -> bool:
        marker = self._settings.app_root / "tmp_volume_fill.bin"
        return not marker.exists()

    async def runtime_config_ok(self) -> bool:
        runtime_env = self._settings.app_root / "config" / "runtime.env"
        if not runtime_env.exists():
            return True
        try:
            lines = runtime_env.read_text(encoding="utf-8").splitlines()
        except OSError:
            return False

        db_path_value: str | None = None
        for raw in lines:
            line = raw.strip()
            if line.startswith("DB_PATH="):
                db_path_value = line.split("=", 1)[1].strip()
                break
        if db_path_value is None or db_path_value == "":
            return True

        expected_runtime = "/target/app/data/app.db"
        expected_local = self._settings.db_file.resolve(strict=False).as_posix()
        return db_path_value in {expected_runtime, expected_local}

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
