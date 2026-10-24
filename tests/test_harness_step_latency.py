from __future__ import annotations

from typing import Any

import pytest

from harness.contracts import contract_errors
from harness.sensors.step_latency import (
    load_step_budgets,
    step_durations,
    step_latency_budget,
)
from tests.harness_scenarios import SCENARIOS

BUDGETS = {"why": 20.0, "how": 20.0, "jev": 8.0, "execute": 10.0, "verify": 10.0}


def _step(ts: float, step: str, *, incident: str = "inc-1", attempt: int = 1) -> dict[str, Any]:
    return {
        "ts": ts,
        "event_type": "workflow.step",
        "payload": {"incident_id": incident, "attempt": attempt, "step": step, "status": "ok"},
    }


def _attempt(start: float, *, why: float = 5.0, incident: str = "inc-1") -> list[dict[str, Any]]:
    """One full-loop attempt: WHY `why` s, HOW 6 s, JEV 2 s, execute 0.5 s, verify 0.25 s."""
    t = start
    rows = [_step(t, "rag.symptom", incident=incident)]
    t += why
    rows.append(_step(t, "llm.why", incident=incident))
    rows.append(_step(t + 1.0, "rag.repair", incident=incident))
    t += 7.0
    rows.append(_step(t, "llm.how", incident=incident))
    rows.append(_step(t, "jev.requested", incident=incident))
    rows.append(_step(t + 2.0, "jev.result", incident=incident))
    rows.append(_step(t + 2.0, "dynamic_tool.registered", incident=incident))
    rows.append(_step(t + 2.5, "dynamic_tool.executed", incident=incident))
    rows.append(_step(t + 2.75, "verification.result", incident=incident))
    return rows


def test_budgets_file_holds_the_approved_values() -> None:
    assert load_step_budgets() == BUDGETS


def test_step_durations_measure_each_span() -> None:
    assert step_durations(_attempt(0.0)) == {
        "why": [5.0],
        "how": [6.0],
        "jev": [2.0],
        "execute": [0.5],
        "verify": [0.25],
    }


def test_step_durations_pair_each_end_with_the_latest_start() -> None:
    # A rejected fast path asks JEV twice in one attempt; a failed WHY call re-runs RAG.
    rows = [
        _step(0.0, "rag.symptom"),
        _step(1.0, "rag.symptom"),
        _step(4.0, "llm.why"),
        _step(4.0, "jev.requested"),
        _step(5.0, "jev.result"),
        _step(6.0, "jev.requested"),
        _step(9.0, "jev.result"),
        _step(9.0, "jev.result"),  # unpaired end: ignored
    ]

    durations = step_durations(rows)

    assert durations["why"] == [3.0]
    assert durations["jev"] == [1.0, 3.0]


def test_step_durations_keep_attempts_and_incidents_apart() -> None:
    rows = [
        _step(0.0, "jev.requested", incident="a"),
        _step(1.0, "jev.requested", incident="b"),
        _step(2.0, "jev.result", incident="a", attempt=2),  # other attempt: no pair
        _step(4.0, "jev.result", incident="b"),
    ]

    assert step_durations(rows)["jev"] == [3.0]


def test_pass_when_every_p90_is_within_budget() -> None:
    result = step_latency_budget(_attempt(0.0), budgets=BUDGETS, now=1.0)

    assert (result["status"], result["checked"], result["violations"]) == ("pass", 5, [])
    assert result["ts"] == 1.0
    assert contract_errors(result, "sensor_result") == []


def test_one_slow_call_does_not_fail_but_a_slow_p90_does() -> None:
    nine_fast_one_slow = [
        row
        for index in range(10)
        for row in _attempt(index * 100.0, why=25.0 if index == 0 else 5.0, incident=f"i{index}")
    ]
    two_slow = [
        row
        for index in range(10)
        for row in _attempt(index * 100.0, why=25.0 if index < 2 else 5.0, incident=f"i{index}")
    ]

    assert step_latency_budget(nine_fast_one_slow, budgets=BUDGETS)["status"] == "pass"
    failed = step_latency_budget(two_slow, budgets=BUDGETS)
    assert failed["status"] == "fail"
    assert [v["detail"] for v in failed["violations"]] == [
        "why p90=25.00s exceeds budget 20s (n=10)"
    ]
    assert contract_errors(failed, "sensor_result") == []


def test_p90_exactly_at_budget_passes() -> None:
    rows = _attempt(0.0, why=20.0)

    assert step_latency_budget(rows, budgets=BUDGETS)["status"] == "pass"


def test_skipped_without_measured_steps() -> None:
    result = step_latency_budget([], budgets=BUDGETS)

    assert (result["status"], result["checked"]) == ("skipped", 0)
    assert contract_errors(result, "sensor_result") == []


@pytest.mark.asyncio
async def test_scripted_full_loop_is_measured(tmp_path: Any) -> None:
    run = await SCENARIOS["full_loop_success"](tmp_path)

    durations = step_durations(run.events)

    assert all(len(durations[span]) == 1 for span in ("why", "how", "jev", "execute", "verify"))
