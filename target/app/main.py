from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException

DB_PATH = Path("/target/app/data/app.db")
LOG_PATH = Path("/target/app/app.log")
SCHEMA_PATH = Path("/target/app/schema.sql")

app = FastAPI(title="MI Demo Target")


def write_log(level: str, message: str, **fields: object) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "ts": datetime.now(tz=UTC).isoformat(),
        "level": level,
        "message": message,
    }
    payload.update(fields)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def ensure_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DB_PATH.exists() and SCHEMA_PATH.exists():
        with sqlite3.connect(DB_PATH) as conn:
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


@app.on_event("startup")
def startup() -> None:
    ensure_db()
    write_log("info", "target app started")


@app.get("/health/deep")
def health_deep() -> dict[str, object]:
    if not DB_PATH.exists():
        write_log(
            "error",
            "database file is missing",
            error="sqlite3.OperationalError: unable to open database file",
        )
        raise HTTPException(status_code=500, detail={"ok": False, "error": "db_missing"})

    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("SELECT 1")
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='widgets'"
            ).fetchone()
        if row is None:
            write_log("error", "schema table missing")
            raise HTTPException(status_code=500, detail={"ok": False, "error": "schema_invalid"})
    except sqlite3.Error as exc:
        write_log("error", "db query failed", error=repr(exc))
        raise HTTPException(status_code=500, detail={"ok": False, "error": "db_error"}) from exc

    write_log("info", "health deep ok")
    return {"ok": True}


@app.post("/write")
def write_demo(value: str) -> dict[str, object]:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT INTO widgets(name) VALUES (?)", (value,))
            conn.commit()
    except sqlite3.Error as exc:
        write_log("error", "write failed", error=repr(exc))
        raise HTTPException(status_code=500, detail={"ok": False, "error": "write_error"}) from exc
    return {"ok": True}
