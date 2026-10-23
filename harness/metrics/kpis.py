"""Reference KPI computation over the runtime event log (spec: harness/metrics/README.md).

Usage:
    python -m harness.metrics.kpis state/events.jsonl
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from harness.mappings.runtime import RuntimeEvent, load_event_log


def _summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "median": None, "p90": None, "max": None}
    ordered = sorted(values)
    p90_rank = math.ceil(0.9 * len(ordered))  # nearest-rank
    return {
        "n": len(ordered),
        "median": statistics.median(ordered),
        "p90": ordered[p90_rank - 1],
        "max": ordered[-1],
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def compute_kpis(events: Iterable[RuntimeEvent]) -> dict[str, Any]:
    started_at: dict[str, float] = {}
    recovered: dict[str, dict[str, Any]] = {}
    closed: set[str] = set()
    attempt_outcomes: Counter[str] = Counter()
    jev_status: Counter[str] = Counter()
    unusable_reasons: Counter[str] = Counter()
    selected_total = 0
    selected_with_scores = 0
    winners_by_class: dict[str, set[str]] = {}

    for event in events:
        event_type = event.get("event_type")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        incident_id = payload.get("incident_id")
        if not isinstance(incident_id, str):
            continue
        ts = event.get("ts")

        if event_type == "incident.started" and isinstance(ts, int | float):
            started_at.setdefault(incident_id, float(ts))
        elif event_type == "incident.recovered" and incident_id not in recovered:
            recovered[incident_id] = {**payload, "_ts": ts}
            class_id = payload.get("class_id")
            action_key = payload.get("action_key")
            if isinstance(class_id, str) and isinstance(action_key, str):
                winners_by_class.setdefault(class_id, set()).add(action_key)
        elif event_type == "incident.closed":
            closed.add(incident_id)
        elif event_type == "decision.selected":
            selected_total += 1
            decision = payload.get("decision")
            if (
                isinstance(decision, dict)
                and _is_number(decision.get("usable_probability"))
                and _is_number(decision.get("risk_score"))
            ):
                selected_with_scores += 1
        elif event_type == "workflow.step":
            step = payload.get("step")
            status = payload.get("status")
            if step == "attempt.result" and isinstance(status, str):
                attempt_outcomes[status] += 1
            elif step == "jev.result" and isinstance(status, str):
                jev_status[status] += 1
                if status == "unusable":
                    unusable_reasons[str(payload.get("reason"))] += 1

    cohort = set(started_at)
    cohort_recovered = cohort & set(recovered)
    self_resolved = (cohort & closed) - cohort_recovered
    open_incidents = cohort - cohort_recovered - self_resolved

    ttr = [
        float(recovered[i]["_ts"]) - started_at[i]
        for i in cohort_recovered
        if _is_number(recovered[i]["_ts"])
    ]
    attempts = [
        float(recovered[i]["attempts"])
        for i in cohort_recovered
        if _is_number(recovered[i].get("attempts"))
    ]
    with_source = [i for i in cohort_recovered if recovered[i].get("source") is not None]
    fast_path = [i for i in with_source if recovered[i].get("source") == "class_memory"]
    jev_total = jev_status["usable"] + jev_status["unusable"]
    request_errors = sum(
        count
        for reason, count in unusable_reasons.items()
        if reason.startswith("jev_request_error")
    )

    return {
        "incidents": {
            "started": len(cohort),
            "recovered": len(cohort_recovered),
            "self_resolved": len(self_resolved),
            "open": len(open_incidents),
        },
        "recovery_rate": _ratio(len(cohort_recovered), len(cohort)),
        "time_to_recover_seconds": _summary(ttr),
        "attempts_per_recovered_incident": _summary(attempts),
        "attempt_outcomes": dict(sorted(attempt_outcomes.items())),
        "decision": {
            "jev_results": jev_total,
            "usable": jev_status["usable"],
            "unusable": jev_status["unusable"],
            "usable_rate": _ratio(jev_status["usable"], jev_total),
            "unusable_reasons": dict(sorted(unusable_reasons.items())),
            "jev_request_error_rate": _ratio(request_errors, jev_total),
            "selected": selected_total,
            "selected_with_scores_rate": _ratio(selected_with_scores, selected_total),
        },
        "fast_path": {
            "recoveries_with_source": len(with_source),
            "fast_path_recoveries": len(fast_path),
            "fast_path_recovery_share": _ratio(len(fast_path), len(with_source)),
        },
        "winning_actions_per_class": {
            class_id: len(keys) for class_id, keys in sorted(winners_by_class.items())
        },
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m harness.metrics.kpis <events.jsonl>", file=sys.stderr)
        return 2
    print(json.dumps(compute_kpis(load_event_log(Path(argv[1]))), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
