from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from harness.metrics.kpis import compute_kpis, main
from tests.harness_scenarios import SCENARIOS


def _event(ts: float, event_type: str, **payload: Any) -> dict[str, Any]:
    return {"ts": ts, "event_type": event_type, "payload": payload}


@pytest.mark.asyncio
async def test_kpis_for_unusable_decisions_then_success(tmp_path: Path) -> None:
    run = await SCENARIOS["unusable_decisions_then_success"](tmp_path)

    kpis = compute_kpis(run.events)

    assert kpis["incidents"] == {"started": 1, "recovered": 1, "self_resolved": 0, "open": 0}
    assert kpis["recovery_rate"] == 1.0
    assert kpis["attempts_per_recovered_incident"]["median"] == 4.0
    assert kpis["time_to_recover_seconds"]["n"] == 1
    assert kpis["time_to_recover_seconds"]["median"] >= 0.0
    assert kpis["attempt_outcomes"] == {"failed": 3, "recovered": 1}
    assert kpis["decision"] == {
        "jev_results": 4,
        "usable": 2,
        "unusable": 2,
        "usable_rate": 0.5,
        "unusable_reasons": {
            "jev_request_error:TypeSafeAPITimeoutError": 1,
            "low_confidence": 1,
        },
        "jev_request_error_rate": 0.25,
        "selected": 1,
        "gate_rejected": 0,
        "selected_with_scores_rate": 1.0,
    }
    # Class memory disabled: recoveries carry no source, so fast-path share is undefined.
    assert kpis["fast_path"] == {
        "recoveries_with_source": 0,
        "fast_path_recoveries": 0,
        "fast_path_recovery_share": None,
    }


@pytest.mark.asyncio
async def test_kpis_for_fast_path_scenarios(tmp_path: Path) -> None:
    failed_fast = await SCENARIOS["fast_path_failure_then_full_loop"](tmp_path)
    kpis = compute_kpis(failed_fast.events)
    assert kpis["fast_path"]["recoveries_with_source"] == 1
    assert kpis["fast_path"]["fast_path_recovery_share"] == 0.0
    assert list(kpis["winning_actions_per_class"].values()) == [1]


@pytest.mark.asyncio
async def test_kpis_fast_path_recovery_share_counts_class_memory_wins(tmp_path: Path) -> None:
    run = await SCENARIOS["fast_path_failure_then_full_loop"](tmp_path)
    events = run.events
    for event in events:
        if event["event_type"] == "incident.recovered":
            event["payload"]["source"] = "class_memory"

    assert compute_kpis(events)["fast_path"]["fast_path_recovery_share"] == 1.0


@pytest.mark.asyncio
async def test_kpis_count_gate_rejections(tmp_path: Path) -> None:
    run = await SCENARIOS["gate_rejected_then_success"](tmp_path)

    kpis = compute_kpis(run.events)

    assert kpis["decision"]["gate_rejected"] == 1
    assert kpis["attempt_outcomes"] == {"failed": 1, "recovered": 1}
    assert kpis["recovery_rate"] == 1.0


@pytest.mark.asyncio
async def test_kpis_count_repair_between_attempts_as_self_resolved(tmp_path: Path) -> None:
    run = await SCENARIOS["repaired_before_retry"](tmp_path)

    kpis = compute_kpis(run.events)

    assert kpis["incidents"] == {"started": 1, "recovered": 0, "self_resolved": 1, "open": 0}
    assert kpis["recovery_rate"] == 0.0
    assert kpis["attempt_outcomes"] == {"failed": 1}


def test_kpis_cohort_classification_and_distribution() -> None:
    events = [
        _event(0.0, "incident.started", incident_id="a"),
        _event(10.0, "incident.recovered", incident_id="a", attempts=1),
        _event(10.0, "incident.closed", incident_id="a", attempts=1),
        _event(20.0, "incident.started", incident_id="b"),
        _event(50.0, "incident.recovered", incident_id="b", attempts=3),
        _event(60.0, "incident.started", incident_id="c"),
        _event(61.0, "incident.closed", incident_id="c", attempts=0),
        _event(70.0, "incident.started", incident_id="d"),
        # Recovered without a start inside the window: excluded from the cohort.
        _event(80.0, "incident.recovered", incident_id="z", attempts=9),
        # Duplicate recovered events keep the first occurrence.
        _event(90.0, "incident.recovered", incident_id="a", attempts=7),
    ]

    kpis = compute_kpis(events)

    assert kpis["incidents"] == {"started": 4, "recovered": 2, "self_resolved": 1, "open": 1}
    assert kpis["recovery_rate"] == 0.5
    assert kpis["time_to_recover_seconds"] == {"n": 2, "median": 20.0, "p90": 30.0, "max": 30.0}
    assert kpis["attempts_per_recovered_incident"] == {
        "n": 2,
        "median": 2.0,
        "p90": 3.0,
        "max": 3.0,
    }


def test_kpis_data_loss_counts_measured_recoveries() -> None:
    events = [
        _event(0.0, "incident.started", incident_id="a"),
        _event(1.0, "incident.recovered", incident_id="a", attempts=1, rows_lost=0),
        _event(2.0, "incident.started", incident_id="b"),
        _event(3.0, "incident.recovered", incident_id="b", attempts=1, rows_lost=4),
        _event(3.5, "incident.started", incident_id="d"),
        _event(3.6, "incident.recovered", incident_id="d", attempts=1, rows_lost=0),
        # Not measured (no healthy reference before the incident).
        _event(4.0, "incident.started", incident_id="c"),
        _event(5.0, "incident.recovered", incident_id="c", attempts=1, rows_lost=None),
    ]

    assert compute_kpis(events)["data_loss"] == {
        "recoveries_measured": 3,
        "lossless_recoveries": 2,
        "rows_lost_total": 4.0,
        "rows_lost_per_recovery": {"n": 3, "median": 0.0, "p90": 4.0, "max": 4.0},
    }


def test_kpis_p90_uses_nearest_rank() -> None:
    events: list[dict[str, Any]] = []
    for i in range(1, 11):
        events.append(_event(0.0, "incident.started", incident_id=f"i{i}"))
        events.append(_event(float(i), "incident.recovered", incident_id=f"i{i}", attempts=i))

    ttr = compute_kpis(events)["time_to_recover_seconds"]

    assert ttr == {"n": 10, "median": 5.5, "p90": 9.0, "max": 10.0}


def test_kpis_empty_log() -> None:
    kpis = compute_kpis([])

    assert kpis["recovery_rate"] is None
    assert kpis["time_to_recover_seconds"] == {"n": 0, "median": None, "p90": None, "max": None}
    assert kpis["decision"]["usable_rate"] is None


@pytest.mark.asyncio
async def test_cli_prints_kpis(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = await SCENARIOS["full_loop_success"](tmp_path)

    assert main(["prog", str(run.state_dir / "events.jsonl")]) == 0
    assert json.loads(capsys.readouterr().out)["recovery_rate"] == 1.0
    assert main(["prog"]) == 2
