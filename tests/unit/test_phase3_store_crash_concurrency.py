from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from mi_agent.state.sqlite_store import SQLiteWALStateStore
from mi_agent.state.store import JSONFileStateStore

pytestmark = pytest.mark.phase3


def test_json_store_survives_kill_mid_write(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "policy.json"
    ready_path = tmp_path / "state" / "ready.flag"

    store = JSONFileStateStore[dict[str, int]](
        state_path,
        default_factory=dict,
        decode=lambda raw: {k: int(v) for k, v in dict(raw).items()},
        encode=lambda state: state,
    )
    store.save({"value": 1})

    script = f"""
import pathlib
import time
import mi_agent.state.store as store_mod

state_path = pathlib.Path({str(state_path)!r})
ready_path = pathlib.Path({str(ready_path)!r})
real_replace = store_mod.os.replace

def slow_replace(src, dst):
    ready_path.parent.mkdir(parents=True, exist_ok=True)
    ready_path.write_text("ready", encoding="utf-8")
    time.sleep(30)
    real_replace(src, dst)

store_mod.os.replace = slow_replace
store = store_mod.JSONFileStateStore(
    state_path,
    default_factory=dict,
    decode=lambda raw: {{k: int(v) for k, v in dict(raw).items()}},
    encode=lambda state: state,
)
store.save({{"value": 2}})
"""

    env = os.environ.copy()
    current_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = "src" if current_path == "" else f"src:{current_path}"
    process = subprocess.Popen([sys.executable, "-c", script], env=env)
    try:
        _wait_for_file(ready_path)
        process.kill()
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    loaded = store.load()
    assert loaded == {"value": 1}
    assert json.loads(state_path.read_text(encoding="utf-8")) == {"value": 1}

    store.save({"value": 3})
    assert store.load() == {"value": 3}


def test_json_store_handles_concurrent_writers(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "skills.json"
    store = JSONFileStateStore[dict[str, int]](
        state_path,
        default_factory=dict,
        decode=lambda raw: {k: int(v) for k, v in dict(raw).items()},
        encode=lambda state: state,
    )

    def write_value(writer_id: int) -> None:
        for _ in range(8):
            store.save({"writer": writer_id})

    with ThreadPoolExecutor(max_workers=6) as executor:
        for writer_id in range(6):
            executor.submit(write_value, writer_id)

    loaded = store.load()
    assert loaded["writer"] in set(range(6))
    assert json.loads(state_path.read_text(encoding="utf-8"))["writer"] in set(range(6))


def test_sqlite_store_survives_kill_mid_transaction(tmp_path: Path) -> None:
    store_path = tmp_path / "state" / "store.sqlite3"
    ready_path = tmp_path / "state" / "sqlite_ready.flag"
    key = "policy/db_file_missing"

    store = SQLiteWALStateStore(store_path, sandbox_root=tmp_path / "target")
    store.save(key, {"value": 1})

    script = f"""
import pathlib
import sqlite3
import time

store_path = pathlib.Path({str(store_path)!r})
ready_path = pathlib.Path({str(ready_path)!r})
key = {key!r}

conn = sqlite3.connect(store_path)
conn.execute("BEGIN IMMEDIATE")
conn.execute("UPDATE state_entries SET payload = ? WHERE key = ?", ('{{"value": 999}}', key))
ready_path.parent.mkdir(parents=True, exist_ok=True)
ready_path.write_text("ready", encoding="utf-8")
time.sleep(30)
"""

    process = subprocess.Popen([sys.executable, "-c", script])
    try:
        _wait_for_file(ready_path)
        process.kill()
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert store.load(key) == {"value": 1}
    with sqlite3.connect(store_path) as conn:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    assert row is not None
    assert str(row[0]).lower() == "ok"


def test_sqlite_store_handles_concurrent_writers(tmp_path: Path) -> None:
    store_path = tmp_path / "state" / "store.sqlite3"
    store = SQLiteWALStateStore(store_path, sandbox_root=tmp_path / "target")
    key = "policy/db_file_missing"

    def write_value(writer_id: int) -> None:
        for _ in range(8):
            store.save(key, {"writer": writer_id}, schema_version=1)

    with ThreadPoolExecutor(max_workers=6) as executor:
        for writer_id in range(6):
            executor.submit(write_value, writer_id)

    loaded = store.load(key)
    assert loaded is not None
    assert loaded["writer"] in set(range(6))
    with sqlite3.connect(store_path) as conn:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    assert row is not None
    assert str(row[0]).lower() == "ok"


def _wait_for_file(path: Path, *, timeout_seconds: float = 10.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {path}")

