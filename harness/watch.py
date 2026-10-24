"""Continuous surveillance: invariant sensors, KPI thresholds, step latency budgets and
token / JEV call budgets over a rolling window.

Prints one `sensor_result` v1 record per line (JSONL) to stdout; harness code never
writes files, so the caller appends the output, e.g. the compose `harness-watch` service:

    python -m harness.watch /state/events.jsonl >> /results/harness/sensor_results.jsonl

The window is the last `window_hours` (thresholds file) before `--now`. An incident with
any event in the window is checked with all its events, so a timeline cut at the window
start is never misread as an invariant violation. Exit status 1 if any sensor fails.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from harness.mappings.runtime import RuntimeEvent, load_event_log
from harness.sensors.call_budgets import call_budgets
from harness.sensors.kpi_thresholds import THRESHOLDS_PATH, kpi_thresholds, load_thresholds
from harness.sensors.runtime_invariants import run_all
from harness.sensors.step_latency import load_step_budgets, step_latency_budget


def _incident_id(event: RuntimeEvent) -> str | None:
    payload = event.get("payload")
    value = payload.get("incident_id") if isinstance(payload, dict) else None
    return value if isinstance(value, str) and value != "" else None


def _ts(event: RuntimeEvent) -> float | None:
    value = event.get("ts")
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def window_events(events: Iterable[RuntimeEvent], *, since: float) -> list[dict[str, Any]]:
    rows = [dict(event) for event in events]
    incidents = {
        incident
        for event in rows
        if (incident := _incident_id(event)) is not None
        and (ts := _ts(event)) is not None
        and ts >= since
    }
    return [
        event
        for event in rows
        if (incident := _incident_id(event)) in incidents
        or (incident is None and (ts := _ts(event)) is not None and ts >= since)
    ]


def watch(
    events: Iterable[RuntimeEvent],
    *,
    thresholds: dict[str, float],
    step_budgets: dict[str, float],
    now: float,
    window_hours: float | None = None,
) -> list[dict[str, Any]]:
    hours = window_hours if window_hours is not None else thresholds["window_hours"]
    rows = window_events(events, since=now - hours * 3600.0)
    # One shared `ts` marks the records of one watch run.
    return [
        *run_all(rows, stage="continuous", now=now),
        kpi_thresholds(rows, thresholds=thresholds, stage="continuous", now=now),
        step_latency_budget(rows, budgets=step_budgets, stage="continuous", now=now),
        *call_budgets(rows, thresholds=thresholds, stage="continuous", now=now),
    ]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m harness.watch",
        description="Print continuous sensor results (JSONL) for a rolling event-log window.",
    )
    parser.add_argument("events", type=Path, help="runtime event log (state/events.jsonl)")
    parser.add_argument("--thresholds", type=Path, default=THRESHOLDS_PATH)
    parser.add_argument("--window-hours", type=float, help="default: thresholds window_hours")
    parser.add_argument("--now", type=float, help="window end, epoch seconds (default: now)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    events = load_event_log(args.events) if args.events.exists() else []
    results = watch(
        events,
        thresholds=load_thresholds(args.thresholds),
        step_budgets=load_step_budgets(args.thresholds),
        now=time.time() if args.now is None else args.now,
        window_hours=args.window_hours,
    )
    for result in results:
        print(json.dumps(result, sort_keys=True))
    return 1 if any(result["status"] == "fail" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
