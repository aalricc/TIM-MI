from __future__ import annotations

from typing import Any

import pytest

from harness.contracts import contract_errors
from harness.sensors.call_budgets import call_budgets
from harness.sensors.kpi_thresholds import load_thresholds


def _event(ts: float, event_type: str, **payload: Any) -> dict[str, Any]:
    return {"ts": ts, "event_type": event_type, "payload": payload}


def _incident(incident_id: str, *, tokens: int, jev_calls: int) -> list[dict[str, Any]]:
    rows = [
        _event(0.0, "incident.started", incident_id=incident_id),
        _event(
            1.0, "workflow.step", incident_id=incident_id, attempt=1, step="llm.why",
            llm_usage_total=tokens,
        ),
    ]
    rows += [
        _event(2.0, "workflow.step", incident_id=incident_id, attempt=1, step="jev.requested")
        for _ in range(jev_calls)
    ]
    return rows


def test_thresholds_set_jev_budget_and_leave_tokens_report_only() -> None:
    thresholds = load_thresholds()

    assert thresholds["max_jev_calls_per_incident_p90"] == 6.0
    assert "max_tokens_per_incident_p90" not in thresholds


def test_token_budget_reports_without_failing_when_no_budget_is_set() -> None:
    events = _incident("a", tokens=50_000, jev_calls=1)

    token, calls = call_budgets(events, thresholds={"max_jev_calls_per_incident_p90": 6}, now=3.0)

    assert token["sensor_name"] == "token_budget"
    assert token["status"] == "pass"
    assert token["message"] == "p90 50000 tokens per incident over 1 incidents (no budget set)"
    assert calls["sensor_name"] == "call_rate_budget"
    assert calls["status"] == "pass"
    assert {token["ts"], calls["ts"]} == {3.0}
    for result in (token, calls):
        assert contract_errors(result, "sensor_result") == []


@pytest.mark.parametrize(("jev_calls", "status"), [(6, "pass"), (7, "fail")])
def test_call_rate_budget_fails_only_above_the_budget(jev_calls: int, status: str) -> None:
    events = _incident("a", tokens=10, jev_calls=jev_calls)

    calls = call_budgets(events, thresholds={"max_jev_calls_per_incident_p90": 6})[1]

    assert calls["status"] == status
    if status == "fail":
        assert calls["severity"] == "error"
        assert [v["detail"] for v in calls["violations"]] == [
            "JEV calls p90=7 exceeds budget 6 (n=1)"
        ]
    assert contract_errors(calls, "sensor_result") == []


def test_token_budget_fails_when_a_budget_is_set_and_exceeded() -> None:
    events = _incident("a", tokens=30_000, jev_calls=1)

    token = call_budgets(events, thresholds={"max_tokens_per_incident_p90": 20_000})[0]

    assert token["status"] == "fail"
    assert token["violations"][0]["detail"] == "tokens p90=30000 exceeds budget 20000 (n=1)"


def test_budgets_skip_without_incidents() -> None:
    results = call_budgets([], thresholds={"max_jev_calls_per_incident_p90": 6})

    assert [(r["status"], r["checked"]) for r in results] == [("skipped", 0), ("skipped", 0)]
    for result in results:
        assert contract_errors(result, "sensor_result") == []
