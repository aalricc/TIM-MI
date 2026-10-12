from __future__ import annotations

import json

import pytest

from mi_agent.events.bus import EventBus
from mi_agent.verifier.verifier import Verifier

pytestmark = pytest.mark.phase1


@pytest.mark.asyncio()
async def test_verifier_is_failure_type_agnostic(test_settings) -> None:
    verifier = Verifier(test_settings, EventBus())
    verifier.health_endpoint_ok = lambda: _async_true()  # type: ignore[method-assign]

    a = await verifier.verify(failure_type="db_file_missing")
    b = await verifier.verify(failure_type="disk_full")

    assert a.healthy == b.healthy
    assert a.details["verifier_version"] == b.details["verifier_version"]


@pytest.mark.asyncio()
async def test_verifier_ledger_continuity_detects_missing_rows(test_settings) -> None:
    verifier = Verifier(test_settings, EventBus())
    verifier.health_endpoint_ok = lambda: _async_true()  # type: ignore[method-assign]

    import sqlite3

    with sqlite3.connect(test_settings.db_file) as conn:
        conn.execute("INSERT INTO widgets(name) VALUES ('present')")
        row_id = int(conn.execute("SELECT id FROM widgets ORDER BY id DESC LIMIT 1").fetchone()[0])
        conn.commit()

    ledger = [
        {"table": "widgets", "pk_column": "id", "pk_value": row_id},
        {"table": "widgets", "pk_column": "id", "pk_value": 99999},
    ]
    (test_settings.state_dir / "ack_ledger.json").parent.mkdir(parents=True, exist_ok=True)
    (test_settings.state_dir / "ack_ledger.json").write_text(
        json.dumps(ledger),
        encoding="utf-8",
    )

    result = await verifier.verify()

    assert result.healthy is False
    continuity = result.details["data_continuity"]
    assert continuity["checked"] == 2
    assert continuity["missing"] == 1
    assert continuity["ok"] is False


@pytest.mark.asyncio()
async def test_verifier_delayed_reverify_affects_final_health(test_settings) -> None:
    test_settings.verifier_delayed_reverify_seconds = 0.001
    verifier = Verifier(test_settings, EventBus())

    ping_values = iter([True, False])

    async def _ping_sequence() -> bool:
        return next(ping_values)

    verifier.ping_db = _ping_sequence  # type: ignore[method-assign]
    verifier.integrity_check = lambda: _async_true()  # type: ignore[method-assign]
    verifier.check_schema = lambda: _async_true()  # type: ignore[method-assign]
    verifier.test_read_write = lambda: _async_true()  # type: ignore[method-assign]
    verifier.health_endpoint_ok = lambda: _async_true()  # type: ignore[method-assign]

    result = await verifier.verify()

    assert result.details["delayed_reverify_ok"] is False
    assert result.healthy is False


async def _async_true() -> bool:
    return True
