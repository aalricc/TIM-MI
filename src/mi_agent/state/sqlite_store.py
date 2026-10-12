from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class SQLiteWALStateStore:
    def __init__(
        self,
        path: Path,
        *,
        sandbox_root: Path,
        migrations: list[str] | None = None,
    ) -> None:
        resolved_path = path.expanduser().resolve()
        resolved_sandbox = sandbox_root.expanduser().resolve()
        if resolved_path.is_relative_to(resolved_sandbox):
            raise ValueError("SQLite state store must live outside the sandbox root")

        self._path = resolved_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._migrations = migrations if migrations is not None else _default_migrations()
        self._bootstrap()

    def save(self, key: str, payload: dict[str, Any], *, schema_version: int = 1) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                """
                INSERT INTO state_entries(key, payload, schema_version)
                VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    payload=excluded.payload,
                    schema_version=excluded.schema_version,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (key, body, schema_version),
            )
            conn.commit()

    def load(self, key: str) -> dict[str, Any] | None:
        with sqlite3.connect(self._path) as conn:
            row = conn.execute(
                "SELECT payload FROM state_entries WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row[0]))
        if not isinstance(payload, dict):
            raise ValueError("Stored payload must decode to a JSON object")
        return payload

    def bootstrap_version(self) -> int:
        with sqlite3.connect(self._path) as conn:
            row = conn.execute("SELECT MAX(version) FROM _migrations").fetchone()
        if row is None or row[0] is None:
            return 0
        return int(row[0])

    def journal_mode(self) -> str:
        with sqlite3.connect(self._path) as conn:
            row = conn.execute("PRAGMA journal_mode").fetchone()
        if row is None:
            return ""
        return str(row[0]).lower()

    def _bootstrap(self) -> None:
        with sqlite3.connect(self._path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS _migrations (version INTEGER PRIMARY KEY)")
            current_row = conn.execute("SELECT MAX(version) FROM _migrations").fetchone()
            current = 0 if current_row is None or current_row[0] is None else int(current_row[0])

            for version, statement in enumerate(self._migrations, start=1):
                if version <= current:
                    continue
                conn.executescript(statement)
                conn.execute("INSERT INTO _migrations(version) VALUES (?)", (version,))
            conn.commit()


def _default_migrations() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS state_entries(
            key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            schema_version INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    ]
