"""Offline checker for approved fault scenario outcomes.

Usage:
    python -m harness.scenarios.check [--stage ci|continuous] <fixture.json> <log-slice.jsonl>
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from harness.contracts import validate_record
from harness.mappings.runtime import RuntimeEvent, decision_traces, load_event_log
from harness.metrics.kpis import compute_kpis
from harness.sensors.runtime_invariants import run_all


def load_fixture(path: Path) -> dict[str, Any]:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(fixture, dict):
        raise ValueError(f"fixture is not a JSON object: {path}")
    validate_record(fixture, "scenario_fixture")
    return fixture


def _incident_id(event: RuntimeEvent) -> str | None:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    value = payload.get("incident_id")
    return value if isinstance(value, str) and value != "" else None


def _attempt(event: RuntimeEvent) -> int | None:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    value = payload.get("attempts")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _ts(event: RuntimeEvent) -> float | None:
    value = event.get("ts")
    if isinstance(value, int | float) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number):
            return number
    return None


def _incident_ids(events: Iterable[RuntimeEvent]) -> list[str]:
    seen: set[str] = set()
    ids: list[str] = []
    for event in events:
        incident_id = _incident_id(event)
        if incident_id is not None and incident_id not in seen:
            seen.add(incident_id)
            ids.append(incident_id)
    return ids


def _selected_trace_index(
    events: Iterable[RuntimeEvent],
) -> dict[tuple[str, int], list[dict[str, Any]]]:
    index: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for trace in decision_traces(events):
        if trace.get("outcome") != "selected":
            continue
        incident_id = trace.get("incident_id")
        attempt = trace.get("attempt")
        if (
            isinstance(incident_id, str)
            and isinstance(attempt, int)
            and not isinstance(attempt, bool)
        ):
            index[(incident_id, attempt)].append(trace)
    return index


def _starts(events: Iterable[RuntimeEvent]) -> dict[str, float]:
    starts: dict[str, float] = {}
    for event in events:
        if event.get("event_type") != "incident.started":
            continue
        incident_id = _incident_id(event)
        ts = _ts(event)
        if incident_id is None or ts is None:
            continue
        current = starts.get(incident_id)
        if current is None or ts < current:
            starts[incident_id] = ts
    return starts


def _recoveries(events: Iterable[RuntimeEvent]) -> dict[str, RuntimeEvent]:
    rows: dict[str, RuntimeEvent] = {}
    for event in events:
        if event.get("event_type") != "incident.recovered":
            continue
        incident_id = _incident_id(event)
        if incident_id is None:
            continue
        current = rows.get(incident_id)
        if current is None:
            rows[incident_id] = event
            continue
        current_ts = _ts(current)
        candidate_ts = _ts(event)
        if current_ts is None or (candidate_ts is not None and candidate_ts > current_ts):
            rows[incident_id] = event
    return rows


def evaluate_fixture(
    fixture: Mapping[str, Any],
    events: Iterable[RuntimeEvent],
    *,
    stage: str,
    now: float | None = None,
) -> dict[str, Any]:
    approved = fixture["approved"]
    approved_operations = set(approved["operations"])
    max_attempts = int(approved["max_attempts"])
    max_ttr_seconds = float(approved["max_ttr_seconds"])

    event_rows = list(events)
    incidents = _incident_ids(event_rows)
    starts = _starts(event_rows)
    recoveries = _recoveries(event_rows)
    selected = _selected_trace_index(event_rows)
    kpis = compute_kpis(event_rows)

    violations: list[dict[str, Any]] = []
    if not incidents:
        violations.append(
            {"incident_id": None, "attempt": None, "detail": "no incidents in log slice"}
        )

    for incident_id in incidents:
        recovered = recoveries.get(incident_id)
        if recovered is None:
            violations.append(
                {"incident_id": incident_id, "attempt": None, "detail": "incident not recovered"}
            )
            continue

        attempts = _attempt(recovered)
        if attempts is None:
            violations.append(
                {
                    "incident_id": incident_id,
                    "attempt": None,
                    "detail": "incident.recovered missing attempts",
                }
            )
            continue
        if attempts > max_attempts:
            violations.append(
                {
                    "incident_id": incident_id,
                    "attempt": attempts,
                    "detail": f"attempts {attempts} exceed max_attempts {max_attempts}",
                }
            )

        start_ts = starts.get(incident_id)
        recovered_ts = _ts(recovered)
        if start_ts is None or recovered_ts is None:
            violations.append(
                {
                    "incident_id": incident_id,
                    "attempt": attempts,
                    "detail": "missing timestamps for TTR computation",
                }
            )
        else:
            ttr = recovered_ts - start_ts
            if ttr < 0:
                violations.append(
                    {
                        "incident_id": incident_id,
                        "attempt": attempts,
                        "detail": f"negative ttr {ttr:.3f}s (recovered before incident start)",
                    }
                )
            elif ttr > max_ttr_seconds:
                violations.append(
                    {
                        "incident_id": incident_id,
                        "attempt": attempts,
                        "detail": f"ttr {ttr:.3f}s exceeds max_ttr_seconds {max_ttr_seconds}",
                    }
                )

        selected_rows = selected.get((incident_id, attempts), [])
        if len(selected_rows) != 1:
            violations.append(
                {
                    "incident_id": incident_id,
                    "attempt": attempts,
                    "detail": (
                        "selected decision traces for recovered attempt: "
                        f"{len(selected_rows)}"
                    ),
                }
            )
            continue

        selected_candidate = selected_rows[0].get("selected_candidate")
        operation = (
            selected_candidate.get("operation")
            if isinstance(selected_candidate, Mapping)
            else None
        )
        if not isinstance(operation, str):
            violations.append(
                {
                    "incident_id": incident_id,
                    "attempt": attempts,
                    "detail": "selected operation missing in decision trace",
                }
            )
        elif operation not in approved_operations:
            violations.append(
                {
                    "incident_id": incident_id,
                    "attempt": attempts,
                    "detail": f"operation {operation!r} not in approved operations",
                }
            )

    kpi_recovered = kpis.get("incidents", {}).get("recovered")
    recovered_count = sum(1 for incident_id in incidents if incident_id in recoveries)
    if isinstance(kpi_recovered, int) and kpi_recovered != recovered_count:
        violations.append(
            {
                "incident_id": None,
                "attempt": None,
                "detail": (
                    "kpi recovered count mismatch: "
                    f"{kpi_recovered} != {recovered_count} for this slice"
                ),
            }
        )

    invariant_results = run_all(event_rows, stage=stage, now=now)
    for result in invariant_results:
        if result.get("status") != "fail":
            continue
        for violation in result.get("violations", []):
            sensor_incident_id = (
                violation.get("incident_id") if isinstance(violation, Mapping) else None
            )
            attempt = violation.get("attempt") if isinstance(violation, Mapping) else None
            detail = (
                violation.get("detail")
                if isinstance(violation, Mapping)
                else "sensor violation"
            )
            violations.append(
                {
                    "incident_id": (
                        sensor_incident_id if isinstance(sensor_incident_id, str) else None
                    ),
                    "attempt": (
                        attempt
                        if isinstance(attempt, int) and not isinstance(attempt, bool)
                        else None
                    ),
                    "detail": f"invariant {result.get('sensor_name')}: {detail}",
                }
            )

    failed = bool(violations)
    outcome = {
        "schema_version": "v1",
        "sensor_name": "approved_recovery_outcomes",
        "lane": "behavior",
        "sensor_type": "computational",
        "stage": stage,
        "status": "fail" if failed else "pass",
        "severity": "error" if failed else "info",
        "message": (
            f"{len(violations)} violation(s) in scenario {fixture['scenario_id']}"
            if failed
            else f"scenario {fixture['scenario_id']} meets approved outcomes"
        ),
        "violations": violations,
        "checked": len(incidents),
        "ts": time.time() if now is None else now,
    }
    validate_record(outcome, "sensor_result")
    return outcome


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m harness.scenarios.check")
    parser.add_argument("fixture_path", help="Path to scenario fixture JSON")
    parser.add_argument("log_path", help="Path to sliced events JSONL")
    parser.add_argument("--stage", choices=("ci", "continuous"), default="ci")
    return parser


def main(argv: list[str]) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    try:
        fixture = load_fixture(Path(args.fixture_path))
        events = load_event_log(Path(args.log_path))
        result = evaluate_fixture(fixture, events, stage=args.stage)
    except (OSError, ValueError, json.JSONDecodeError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(json.dumps(result, indent=2))
    return 1 if result["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
