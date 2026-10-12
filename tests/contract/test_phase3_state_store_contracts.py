from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

from mi_agent.state.postgres_store import PostgresStateStoreStub
from mi_agent.state.sqlite_store import SQLiteWALStateStore

pytestmark = pytest.mark.phase3


def test_sqlite_wal_store_bootstraps_migration_and_wal_mode(tmp_path: Path) -> None:
    sandbox_root = tmp_path / "target"
    store_path = tmp_path / "state" / "state_store.sqlite3"

    store = SQLiteWALStateStore(store_path, sandbox_root=sandbox_root)

    assert store.bootstrap_version() == 1
    assert store.journal_mode() == "wal"

    with sqlite3.connect(store_path) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='state_entries'"
        ).fetchone()
    assert row is not None


def test_sqlite_wal_store_rejects_paths_inside_sandbox(tmp_path: Path) -> None:
    sandbox_root = tmp_path / "target"
    bad_path = sandbox_root / "state.sqlite3"

    with pytest.raises(ValueError):
        SQLiteWALStateStore(bad_path, sandbox_root=sandbox_root)


def test_sqlite_wal_store_round_trip(tmp_path: Path) -> None:
    store = SQLiteWALStateStore(
        tmp_path / "state" / "state_store.sqlite3",
        sandbox_root=tmp_path / "target",
    )
    payload = {"avg_reward": 1.2, "visits": 3}
    store.save("policy/db_file_missing", payload, schema_version=2)

    loaded = store.load("policy/db_file_missing")
    assert loaded == payload


def test_postgres_stub_matches_sqlite_contract_signatures() -> None:
    sqlite_save = inspect.signature(SQLiteWALStateStore.save)
    sqlite_load = inspect.signature(SQLiteWALStateStore.load)
    pg_save = inspect.signature(PostgresStateStoreStub.save)
    pg_load = inspect.signature(PostgresStateStoreStub.load)

    assert sqlite_save == pg_save
    assert sqlite_load == pg_load


def test_postgres_stub_methods_raise_not_implemented() -> None:
    stub = PostgresStateStoreStub("postgresql://example.invalid/mi")

    with pytest.raises(NotImplementedError):
        stub.save("policy/db_file_missing", {"value": 1.0})
    with pytest.raises(NotImplementedError):
        stub.load("policy/db_file_missing")

