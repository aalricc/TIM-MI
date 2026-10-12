from __future__ import annotations

import json
import sqlite3

import pytest

from mi_agent.events.bus import EventBus
from mi_agent.verifier.verifier import Verifier

pytestmark = pytest.mark.phase1


def _write_ledger(test_settings, entries: list[dict[str, object]]) -> None:
    ledger_path = test_settings.state_dir / "ack_ledger.json"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps(entries), encoding="utf-8")


@pytest.mark.asyncio()
async def test_verifier_rejects_empty_db_with_schema_when_ledger_has_expected_rows(
    test_settings,
) -> None:
    verifier = Verifier(test_settings, EventBus())
    verifier.health_endpoint_ok = lambda: _async_true()  # type: ignore[method-assign]

    _write_ledger(
        test_settings,
        [{"table": "widgets", "pk_column": "id", "pk_value": 1}],
    )

    with sqlite3.connect(test_settings.db_file) as conn:
        conn.execute("DELETE FROM widgets")
        conn.commit()

    result = await verifier.verify()

    assert result.healthy is False
    continuity = result.details["data_continuity"]
    assert continuity["checked"] == 1
    assert continuity["missing"] == 1
    assert continuity["ok"] is False


@pytest.mark.asyncio()
async def test_verifier_rejects_too_old_restore_against_ledger(test_settings, tmp_path) -> None:
    verifier = Verifier(test_settings, EventBus())
    verifier.health_endpoint_ok = lambda: _async_true()  # type: ignore[method-assign]

    with sqlite3.connect(test_settings.db_file) as conn:
        conn.execute("DELETE FROM widgets")
        conn.execute("INSERT INTO widgets(name) VALUES ('old')")
        old_id = int(conn.execute("SELECT id FROM widgets ORDER BY id DESC LIMIT 1").fetchone()[0])
        conn.execute("INSERT INTO widgets(name) VALUES ('new')")
        new_id = int(conn.execute("SELECT id FROM widgets ORDER BY id DESC LIMIT 1").fetchone()[0])
        conn.commit()

    _write_ledger(
        test_settings,
        [{"table": "widgets", "pk_column": "id", "pk_value": new_id}],
    )

    old_backup = tmp_path / "old.db"
    with sqlite3.connect(old_backup) as conn:
        conn.execute("CREATE TABLE widgets(id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        conn.execute("INSERT INTO widgets(id, name) VALUES (?, 'old')", (old_id,))
        conn.commit()

    old_backup.replace(test_settings.db_file)

    result = await verifier.verify()

    assert result.healthy is False
    continuity = result.details["data_continuity"]
    assert continuity["checked"] == 1
    assert continuity["missing"] == 1
    assert continuity["ok"] is False


async def _async_true() -> bool:
    return True
