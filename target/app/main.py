from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

APP_ROOT = Path(os.getenv("TARGET_APP_ROOT", "/target/app"))
DB_PATH = Path(os.getenv("TARGET_DB_PATH", str(APP_ROOT / "data" / "app.db")))
LOG_PATH = Path(os.getenv("TARGET_LOG_PATH", str(APP_ROOT / "app.log")))
SCHEMA_PATH = Path(os.getenv("TARGET_SCHEMA_PATH", str(APP_ROOT / "schema.sql")))
HEARTBEAT_INTERVAL_SECONDS = max(1.0, float(os.getenv("TARGET_HEARTBEAT_INTERVAL_SECONDS", "3")))

# Columns the widget endpoints read; deep health fails if any is missing so a repair
# that recreates the table with the wrong shape is not reported healthy.
REQUIRED_WIDGET_COLUMNS = frozenset({"id", "name", "created_at"})

_heartbeat_task: asyncio.Task[None] | None = None


class WidgetCreateRequest(BaseModel):
    value: str = Field(min_length=1, max_length=255)


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
            conn.commit()


def sqlite_failure(exc: sqlite3.Error) -> str:
    text = str(exc).lower()
    if "malformed" in text or "file is not a database" in text:
        return "db_corrupt"
    if "no such table" in text:
        return "schema_invalid"
    if "unable to open database file" in text:
        return "db_missing"
    return "db_error"


def db_status() -> dict[str, object]:
    status: dict[str, object] = {
        "db_path": DB_PATH.as_posix(),
        "exists": DB_PATH.exists(),
        "size_bytes": DB_PATH.stat().st_size if DB_PATH.exists() else 0,
    }
    if not DB_PATH.exists():
        status.update({"ok": False, "error": "db_missing"})
        return status

    try:
        with sqlite3.connect(DB_PATH) as conn:
            integrity_rows = conn.execute("PRAGMA integrity_check;").fetchall()
            table_rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='widgets'"
            ).fetchall()
            column_rows = conn.execute("PRAGMA table_info(widgets);").fetchall()
    except sqlite3.Error as exc:
        status.update({"ok": False, "error": sqlite_failure(exc), "details": repr(exc)})
        return status

    integrity = [str(row[0]) for row in integrity_rows]
    integrity_ok = all(item.lower() == "ok" for item in integrity)
    table_present = len(table_rows) > 0
    missing_columns = sorted(REQUIRED_WIDGET_COLUMNS - {str(row[1]) for row in column_rows})
    status.update(
        {
            "ok": integrity_ok and table_present and not missing_columns,
            "integrity": integrity,
            "widgets_table_present": table_present,
            "widgets_columns_missing": missing_columns,
        }
    )
    if not bool(status["ok"]):
        if not table_present:
            status["error"] = "schema_invalid"
        elif not integrity_ok:
            status["error"] = "db_corrupt"
        else:
            status["error"] = "schema_invalid"
    return status


async def heartbeat_loop() -> None:
    while True:
        state = db_status()
        level = "info" if bool(state.get("ok", False)) else "error"
        write_log(level, "heartbeat", state=state)
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global _heartbeat_task
    ensure_db()
    write_log("info", "target app started", heartbeat_interval=HEARTBEAT_INTERVAL_SECONDS)
    _heartbeat_task = asyncio.create_task(heartbeat_loop())
    try:
        yield
    finally:
        if _heartbeat_task is not None:
            _heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await _heartbeat_task
            _heartbeat_task = None


app = FastAPI(title="MI Demo Target", lifespan=lifespan)


@app.get("/health/live")
def health_live() -> dict[str, object]:
    return {"ok": True}


@app.get("/health/deep")
def health_deep() -> dict[str, object]:
    status = db_status()
    if not bool(status.get("ok", False)):
        error_code = str(status.get("error") or "db_error")
        write_log("error", "health deep failed", error=error_code, status=status)
        raise HTTPException(
            status_code=500,
            detail={"ok": False, "error": error_code, "status": status},
        )

    write_log("info", "health deep ok", db_size=status.get("size_bytes", 0))
    return {"ok": True, "status": status}


@app.get("/ops/logs")
def ops_logs(lines: int = Query(default=200, ge=1, le=5000)) -> dict[str, object]:
    if not LOG_PATH.exists():
        return {"lines": []}
    content = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"lines": content[-lines:]}


@app.get("/ops/db/status")
def ops_db_status() -> dict[str, object]:
    return db_status()


@app.post("/widgets")
def create_widget(payload: WidgetCreateRequest) -> dict[str, object]:
    value = payload.value.strip()
    if value == "":
        raise HTTPException(status_code=400, detail={"ok": False, "error": "empty_value"})

    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.execute("INSERT INTO widgets(name) VALUES (?)", (value,))
            conn.commit()
            widget_id = int(cursor.lastrowid)
    except sqlite3.Error as exc:
        error_code = sqlite_failure(exc)
        write_log("error", "write failed", error=error_code, details=repr(exc), value=value)
        raise HTTPException(status_code=500, detail={"ok": False, "error": error_code}) from exc

    write_log("info", "write succeeded", widget_id=widget_id, value=value)
    return {"ok": True, "widget_id": widget_id, "value": value}


@app.get("/widgets")
def list_widgets(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, object]:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT id, name, created_at FROM widgets ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    except sqlite3.Error as exc:
        error_code = sqlite_failure(exc)
        write_log("error", "list widgets failed", error=error_code, details=repr(exc))
        raise HTTPException(status_code=500, detail={"ok": False, "error": error_code}) from exc

    items = [
        {"id": int(row[0]), "value": str(row[1]), "created_at": str(row[2])}
        for row in rows
    ]
    return {"ok": True, "items": items}


@app.post("/write")
def write_demo(value: str) -> dict[str, object]:
    return create_widget(WidgetCreateRequest(value=value))


@app.post("/ops/fault/{fault}")
def inject_fault(fault: str) -> dict[str, object]:
    if fault == "delete_db":
        if DB_PATH.exists():
            DB_PATH.unlink()
        write_log("error", "fault injected", fault=fault)
        return {"ok": True, "fault": fault}

    if fault == "corrupt_db":
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        DB_PATH.write_bytes(b"not-a-valid-sqlite-db")
        write_log("error", "fault injected", fault=fault)
        return {"ok": True, "fault": fault}

    if fault == "drop_widgets_table":
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DROP TABLE IF EXISTS widgets")
            conn.commit()
        write_log("error", "fault injected", fault=fault)
        return {"ok": True, "fault": fault}

    raise HTTPException(status_code=400, detail={"ok": False, "error": "unknown_fault"})
