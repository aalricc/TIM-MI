from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from harness.contracts import contract_errors
from harness.sensors.kpi_thresholds import THRESHOLDS_PATH, kpi_thresholds, load_thresholds
from harness.watch import main, watch, window_events
from tests.harness_scenarios import SCENARIOS

THRESHOLDS = {
    "window_hours": 24.0,
    "min_recovery_rate": 0.9,
    "max_ttr_p90_seconds": 60.0,
    "max_request_error_rate": 0.1,
}


def _event(ts: float, event_type: str, **payload: Any) -> dict[str, Any]:
    return {"ts": ts, "event_type": event_type, "payload": payload}


def _incident(incident_id: str, start: float, ttr: float | None) -> list[dict[str, Any]]:
    rows = [_event(start, "incident.started", incident_id=incident_id)]
    if ttr is not None:
        rows.append(_event(start + ttr, "incident.recovered", incident_id=incident_id, attempts=1))
    return rows


def _jev(ts: float, status: str, reason: str | None = None) -> dict[str, Any]:
    return _event(
        ts, "workflow.step", incident_id="j", attempt=1, step="jev.result", status=status,
        reason=reason,
    )


def test_thresholds_file_holds_the_approved_values() -> None:
    assert load_thresholds() == THRESHOLDS
    assert Path("harness/metrics/thresholds.json").resolve() == THRESHOLDS_PATH


def test_kpi_thresholds_pass_when_every_kpi_is_within_bounds() -> None:
    events = [*_incident("a", 0.0, 10.0), *_incident("b", 100.0, 20.0), _jev(1.0, "usable")]

    result = kpi_thresholds(events, thresholds=THRESHOLDS, now=5.0)

    assert (result["status"], result["checked"], result["violations"]) == ("pass", 3, [])
    assert result["lane"] == "behavior" and result["stage"] == "continuous"
    assert result["ts"] == 5.0
    assert contract_errors(result, "sensor_result") == []


@pytest.mark.parametrize(
    ("events", "breached"),
    [
        # 1 of 2 recovered: recovery rate 0.5 < 0.9.
        ([*_incident("a", 0.0, 5.0), *_incident("b", 10.0, None)], "recovery_rate=0.5"),
        # p90 TTR 61 s > 60 s.
        ([*_incident("a", 0.0, 61.0)], "ttr_p90_seconds=61"),
        # 1 of 2 JEV results is a request error: 0.5 > 0.1.
        (
            [
                *_incident("a", 0.0, 5.0),
                _jev(1.0, "usable"),
                _jev(2.0, "unusable", "jev_request_error:Timeout"),
            ],
            "jev_request_error_rate=0.5",
        ),
    ],
)
def test_kpi_thresholds_fail_names_the_breached_kpi(
    events: list[dict[str, Any]], breached: str
) -> None:
    result = kpi_thresholds(events, thresholds=THRESHOLDS, now=0.0)

    assert result["status"] == "fail"
    assert result["severity"] == "error"
    assert [v["detail"].split(" ")[0] for v in result["violations"]] == [breached]
    assert contract_errors(result, "sensor_result") == []


def test_kpi_thresholds_values_exactly_at_the_threshold_pass() -> None:
    # 9 of 10 recovered (rate 0.9), p90 TTR exactly 60 s, 1 of 10 JEV results errored.
    events = [*_incident("x", 0.0, None)]
    for index in range(9):
        events += _incident(f"i{index}", float(index), 60.0)
    events += [_jev(1.0, "usable") for _ in range(9)]
    events.append(_jev(2.0, "unusable", "jev_request_error:Timeout"))

    result = kpi_thresholds(events, thresholds=THRESHOLDS, now=0.0)

    assert (result["status"], result["checked"]) == ("pass", 3)


def test_kpi_thresholds_skip_when_no_kpi_is_defined() -> None:
    result = kpi_thresholds([], thresholds=THRESHOLDS, now=0.0)

    assert (result["status"], result["checked"]) == ("skipped", 0)
    assert contract_errors(result, "sensor_result") == []


def test_window_keeps_whole_incidents_that_touch_the_window() -> None:
    events = [
        _event(10.0, "monitor.health", healthy=True),  # before the window: dropped
        *_incident("old", 20.0, 5.0),  # entirely before the window: dropped
        *_incident("spanning", 90.0, 20.0),  # starts before, recovers inside: kept whole
        _event(150.0, "monitor.health", healthy=True),  # inside: kept
    ]

    rows = window_events(events, since=100.0)

    assert [(row["ts"], row["event_type"]) for row in rows] == [
        (90.0, "incident.started"),
        (110.0, "incident.recovered"),
        (150.0, "monitor.health"),
    ]


@pytest.mark.asyncio
async def test_watch_reports_invariants_and_kpis_with_one_shared_ts(tmp_path: Path) -> None:
    run = await SCENARIOS["full_loop_success"](tmp_path)
    now = max(float(event["ts"]) for event in run.events) + 1.0

    results = watch(run.events, thresholds=THRESHOLDS, now=now)

    assert [r["sensor_name"] for r in results][-1] == "kpi_thresholds"
    assert len(results) == 9
    assert {r["ts"] for r in results} == {now}
    assert {r["stage"] for r in results} == {"continuous"}
    assert {r["status"] for r in results} <= {"pass", "skipped"}
    assert results[-1]["status"] == "pass"
    for result in results:
        assert contract_errors(result, "sensor_result") == []


@pytest.mark.asyncio
async def test_watch_ignores_incidents_older_than_the_window(tmp_path: Path) -> None:
    run = await SCENARIOS["full_loop_success"](tmp_path)
    now = max(float(event["ts"]) for event in run.events) + 25 * 3600.0

    kpi = watch(run.events, thresholds=THRESHOLDS, now=now)[-1]

    assert kpi["status"] == "skipped"
    assert kpi["message"] == "no KPI defined over the window (0 incidents)"


def test_cli_prints_jsonl_and_exits_1_on_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "events.jsonl"
    log.write_text(
        "\n".join(json.dumps(e) for e in [*_incident("a", 0.0, 5.0), *_incident("b", 1.0, None)]),
        encoding="utf-8",
    )

    code = main([str(log), "--now", "100"])

    lines = capsys.readouterr().out.splitlines()
    records = [json.loads(line) for line in lines]
    assert code == 1
    assert len(records) == 9
    assert records[-1]["sensor_name"] == "kpi_thresholds"
    assert records[-1]["status"] == "fail"


def test_cli_with_missing_log_reports_without_failing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main([str(tmp_path / "missing.jsonl"), "--now", "100"])

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == 0
    assert records[-1]["status"] == "skipped"


def test_compose_runs_the_watcher_with_read_only_state() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    service = compose.split("  harness-watch:\n", 1)[1].split("\n\n", 1)[0]

    assert "python -m harness.watch /state/events.jsonl >> /results/harness/" in service
    assert "- ./state:/state:ro" in service
    assert "HARNESS_WATCH_INTERVAL_SECONDS:-300" in service
