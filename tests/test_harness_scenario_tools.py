from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from harness.contracts import contract_errors
from harness.events.slice import main as slice_main
from harness.events.slice import slice_events
from harness.scenarios.check import evaluate_fixture, load_fixture
from harness.scenarios.check import main as check_main
from tests.harness_scenarios import SCENARIOS

SCENARIO_DIR = Path("harness/scenarios")
REQUEST_BACKUP_RESTORE = "request_backup_restore"
RUN_SQL = "run_sql"
REPLACE_FILE = "replace_file"

EXPECTED_FIXTURES = {
    "corrupt_db.json": {
        "fault": "corrupt_db",
        "operations": [REQUEST_BACKUP_RESTORE, REPLACE_FILE],
        "source": "docs/runbooks/corrupt_db.md",
    },
    "delete_db.json": {
        "fault": "delete_db",
        "operations": [REQUEST_BACKUP_RESTORE, REPLACE_FILE],
        "source": "docs/runbooks/delete_db.md",
    },
    "drop_widgets_table.json": {
        "fault": "drop_widgets_table",
        "operations": [RUN_SQL, REQUEST_BACKUP_RESTORE],
        "source": "docs/runbooks/drop_widgets_table.md",
    },
}


def _write_jsonl(path: Path, events: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "name",
    ["drop_widgets_table.json", "delete_db.json", "corrupt_db.json"],
)
def test_scenario_fixtures_validate_against_contract(name: str) -> None:
    fixture = json.loads((SCENARIO_DIR / name).read_text(encoding="utf-8"))

    assert contract_errors(fixture, "scenario_fixture") == []


def test_scenario_fixture_set_and_runbook_policy_are_exact() -> None:
    fixture_names = sorted(path.name for path in SCENARIO_DIR.glob("*.json"))
    assert fixture_names == sorted(EXPECTED_FIXTURES)

    for name, expected in EXPECTED_FIXTURES.items():
        fixture = load_fixture(SCENARIO_DIR / name)
        assert fixture["fault"] == expected["fault"]
        assert fixture["approved"]["operations"] == expected["operations"]
        assert fixture["approved"]["max_attempts"] == 3
        assert fixture["approved"]["max_ttr_seconds"] == 300
        assert fixture["source"] == expected["source"]
        assert Path(fixture["source"]).exists()


@pytest.mark.asyncio
async def test_checker_passes_with_approved_operation(tmp_path: Path) -> None:
    run = await SCENARIOS["full_loop_success"](tmp_path)
    fixture = load_fixture(SCENARIO_DIR / "drop_widgets_table.json")

    result = evaluate_fixture(fixture, run.events, stage="ci", now=1.0)

    assert result["status"] == "pass"
    assert result["lane"] == "behavior"
    assert result["stage"] == "ci"
    assert result["checked"] == 1
    assert contract_errors(result, "sensor_result") == []


def _with_selected_operation(events: list[dict[str, Any]], operation: str) -> list[dict[str, Any]]:
    copied = [dict(event) for event in events]
    selected = next(event for event in copied if event["event_type"] == "decision.selected")
    payload = dict(selected["payload"])
    payload["selected_operation"] = operation
    selected["payload"] = payload
    return copied


@pytest.mark.asyncio
async def test_checker_passes_delete_db_fixture_with_approved_operation(tmp_path: Path) -> None:
    run = await SCENARIOS["full_loop_success"](tmp_path)
    fixture = load_fixture(SCENARIO_DIR / "delete_db.json")

    result = evaluate_fixture(
        fixture,
        _with_selected_operation(run.events, REQUEST_BACKUP_RESTORE),
        stage="ci",
        now=1.0,
    )

    assert result["status"] == "pass"
    assert result["checked"] == 1
    assert result["violations"] == []
    assert contract_errors(result, "sensor_result") == []


@pytest.mark.asyncio
async def test_checker_passes_corrupt_db_fixture_with_approved_operation(tmp_path: Path) -> None:
    run = await SCENARIOS["full_loop_success"](tmp_path)
    fixture = load_fixture(SCENARIO_DIR / "corrupt_db.json")

    result = evaluate_fixture(
        fixture,
        _with_selected_operation(run.events, REQUEST_BACKUP_RESTORE),
        stage="ci",
        now=1.0,
    )

    assert result["status"] == "pass"
    assert result["checked"] == 1
    assert result["violations"] == []
    assert contract_errors(result, "sensor_result") == []


@pytest.mark.asyncio
async def test_checker_fails_when_incident_not_recovered(tmp_path: Path) -> None:
    run = await SCENARIOS["full_loop_success"](tmp_path)
    fixture = load_fixture(SCENARIO_DIR / "drop_widgets_table.json")
    events = [event for event in run.events if event.get("event_type") != "incident.recovered"]

    result = evaluate_fixture(fixture, events, stage="ci", now=1.0)

    assert result["status"] == "fail"
    assert any("incident not recovered" in v["detail"] for v in result["violations"])


@pytest.mark.asyncio
async def test_checker_fails_when_attempts_exceed_budget(tmp_path: Path) -> None:
    run = await SCENARIOS["unusable_decisions_then_success"](tmp_path)
    fixture = load_fixture(SCENARIO_DIR / "drop_widgets_table.json")

    result = evaluate_fixture(fixture, run.events, stage="ci", now=1.0)

    assert result["status"] == "fail"
    assert any("exceed max_attempts" in v["detail"] for v in result["violations"])


@pytest.mark.asyncio
async def test_checker_fails_when_ttr_exceeds_budget(tmp_path: Path) -> None:
    run = await SCENARIOS["full_loop_success"](tmp_path)
    fixture = load_fixture(SCENARIO_DIR / "drop_widgets_table.json")
    events = [dict(event) for event in run.events]
    start_ts = next(event["ts"] for event in events if event["event_type"] == "incident.started")
    recovered = next(event for event in events if event["event_type"] == "incident.recovered")
    recovered["ts"] = float(start_ts) + 301.0

    result = evaluate_fixture(fixture, events, stage="ci", now=1.0)

    assert result["status"] == "fail"
    assert any("exceeds max_ttr_seconds" in v["detail"] for v in result["violations"])


@pytest.mark.asyncio
async def test_checker_fails_when_ttr_is_negative(tmp_path: Path) -> None:
    run = await SCENARIOS["full_loop_success"](tmp_path)
    fixture = load_fixture(SCENARIO_DIR / "drop_widgets_table.json")
    events = [dict(event) for event in run.events]
    start_ts = next(event["ts"] for event in events if event["event_type"] == "incident.started")
    recovered = next(event for event in events if event["event_type"] == "incident.recovered")
    recovered["ts"] = float(start_ts) - 1.0

    result = evaluate_fixture(fixture, events, stage="ci", now=1.0)

    assert result["status"] == "fail"
    assert any("negative ttr" in v["detail"] for v in result["violations"])


@pytest.mark.asyncio
async def test_checker_fails_on_unapproved_operation(tmp_path: Path) -> None:
    run = await SCENARIOS["full_loop_success"](tmp_path)
    fixture = load_fixture(SCENARIO_DIR / "drop_widgets_table.json")
    events = [dict(event) for event in run.events]
    selected = next(event for event in events if event["event_type"] == "decision.selected")
    selected_payload = dict(selected["payload"])
    selected_payload["selected_operation"] = "restart_service"
    selected["payload"] = selected_payload

    result = evaluate_fixture(fixture, events, stage="ci", now=1.0)

    assert result["status"] == "fail"
    assert any("not in approved operations" in v["detail"] for v in result["violations"])


@pytest.mark.asyncio
async def test_checker_fails_on_invariant_violation(tmp_path: Path) -> None:
    run = await SCENARIOS["full_loop_success"](tmp_path)
    fixture = load_fixture(SCENARIO_DIR / "drop_widgets_table.json")
    events = [
        event
        for event in run.events
        if not (
            event.get("event_type") == "workflow.step"
            and event.get("payload", {}).get("step") == "verification.result"
        )
    ]

    result = evaluate_fixture(fixture, events, stage="ci", now=1.0)

    assert result["status"] == "fail"
    assert any(
        "invariant verification_follows_execution" in v["detail"]
        for v in result["violations"]
    )


@pytest.mark.asyncio
async def test_checker_uses_earliest_start_and_latest_recovery(tmp_path: Path) -> None:
    run = await SCENARIOS["full_loop_success"](tmp_path)
    fixture = load_fixture(SCENARIO_DIR / "drop_widgets_table.json")
    incident = next(
        event["payload"]["incident_id"]
        for event in run.events
        if event.get("event_type") == "incident.started"
    )
    started = next(
        event
        for event in run.events
        if event.get("event_type") == "incident.started"
        and event.get("payload", {}).get("incident_id") == incident
    )
    recovered = next(
        event
        for event in run.events
        if event.get("event_type") == "incident.recovered"
        and event.get("payload", {}).get("incident_id") == incident
    )

    stale_recovered = dict(recovered)
    stale_payload = dict(stale_recovered["payload"])
    stale_payload["attempts"] = 99
    stale_recovered["payload"] = stale_payload
    stale_recovered["ts"] = float(started["ts"]) - 1.0

    late_start = dict(started)
    late_start["ts"] = float(recovered["ts"]) + 10.0

    events = [late_start, stale_recovered, *run.events]
    result = evaluate_fixture(fixture, events, stage="ci", now=1.0)

    assert result["status"] == "pass"
    assert result["violations"] == []


def test_slice_events_skips_non_finite_timestamps() -> None:
    events: list[dict[str, Any]] = [
        {"ts": 1.0, "event_type": "incident.started", "payload": {"incident_id": "inc-1"}},
        {"ts": float("nan"), "event_type": "incident.started", "payload": {"incident_id": "inc-1"}},
        {"ts": float("inf"), "event_type": "incident.started", "payload": {"incident_id": "inc-1"}},
    ]

    sliced = slice_events(events, since=0.0, until=2.0, incident="inc-1")

    assert len(sliced) == 1
    assert float(sliced[0]["ts"]) == 1.0


@pytest.mark.asyncio
async def test_slice_events_supports_time_window_and_incident_filter(tmp_path: Path) -> None:
    run = await SCENARIOS["failure_then_unhealthy_ping_then_success"](tmp_path)
    incident = next(
        event["payload"]["incident_id"]
        for event in run.events
        if event.get("event_type") == "incident.started"
    )
    incident_timestamps = [
        float(event["ts"])
        for event in run.events
        if event.get("payload", {}).get("incident_id") == incident
    ]
    assert len(incident_timestamps) >= 3
    since = incident_timestamps[1]
    until = incident_timestamps[-2]

    sliced = slice_events(run.events, since=since, until=until, incident=incident)

    assert sliced
    assert any(float(event["ts"]) == since for event in sliced)
    assert any(float(event["ts"]) == until for event in sliced)
    assert all(since <= float(event["ts"]) <= until for event in sliced)
    assert all(event.get("payload", {}).get("incident_id") == incident for event in sliced)


@pytest.mark.asyncio
async def test_slice_cli_boundaries_and_exit_codes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = await SCENARIOS["full_loop_success"](tmp_path)
    log = _write_jsonl(tmp_path / "events.jsonl", run.events)
    incident = next(
        event["payload"]["incident_id"]
        for event in run.events
        if event.get("event_type") == "incident.started"
    )
    incident_timestamps = [
        float(event["ts"])
        for event in run.events
        if event.get("payload", {}).get("incident_id") == incident
    ]
    since_ts = incident_timestamps[0]
    until_ts = incident_timestamps[-1]
    since_iso = datetime.fromtimestamp(since_ts, tz=UTC).isoformat()
    until_iso = datetime.fromtimestamp(until_ts, tz=UTC).isoformat()

    assert (
        slice_main(
            [
                "prog",
                "--since",
                since_iso,
                "--until",
                until_iso,
                "--incident",
                incident,
                str(log),
            ]
        )
        == 0
    )
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines
    parsed_lines = [json.loads(line) for line in lines]
    since_bound = datetime.fromisoformat(since_iso).timestamp()
    until_bound = datetime.fromisoformat(until_iso).timestamp()
    assert parsed_lines == slice_events(
        run.events,
        since=since_bound,
        until=until_bound,
        incident=incident,
    )

    assert (
        slice_main(
            [
                "prog",
                "--since",
                str(since_ts),
                "--until",
                str(until_ts),
                "--incident",
                incident,
                str(log),
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert slice_main(["prog", "--help"]) == 0
    capsys.readouterr()

    bad_json = tmp_path / "bad.jsonl"
    bad_json.write_text("{\n", encoding="utf-8")
    assert slice_main(["prog", str(bad_json)]) == 2

    bad_utf8 = tmp_path / "bad-utf8.jsonl"
    bad_utf8.write_bytes(b"\xff\xfe")
    assert slice_main(["prog", str(bad_utf8)]) == 2

    assert slice_main(["prog", "--since", "not-a-time", str(log)]) == 2
    assert slice_main(["prog", "--since", "3", "--until", "2", str(log)]) == 2


@pytest.mark.asyncio
async def test_check_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = await SCENARIOS["full_loop_success"](tmp_path)
    good_log = _write_jsonl(tmp_path / "good.jsonl", run.events)
    bad_events = [event for event in run.events if event.get("event_type") != "incident.recovered"]
    bad_log = _write_jsonl(tmp_path / "bad.jsonl", bad_events)
    fixture = SCENARIO_DIR / "drop_widgets_table.json"

    assert check_main(["prog", str(fixture), str(good_log)]) == 0
    good = json.loads(capsys.readouterr().out)
    assert good["status"] == "pass"
    assert (good["lane"], good["stage"]) == ("behavior", "ci")
    assert contract_errors(good, "sensor_result") == []

    assert check_main(["prog", "--stage", "continuous", str(fixture), str(good_log)]) == 0
    continuous = json.loads(capsys.readouterr().out)
    assert continuous["stage"] == "continuous"

    assert check_main(["prog", str(fixture), str(bad_log)]) == 1
    bad = json.loads(capsys.readouterr().out)
    assert bad["status"] == "fail"

    assert check_main(["prog", "--help"]) == 0
    capsys.readouterr()
    assert check_main(["prog"]) == 2
