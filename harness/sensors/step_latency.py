"""Behaviour sensor: per-step latency p90 over a log window against budgets.

A step's duration is the gap between two `workflow.step` events of the same attempt:

| Step | From | To |
| --- | --- | --- |
| why | `rag.symptom` | `llm.why` |
| how | `rag.repair` | `llm.how` |
| jev | `jev.requested` | `jev.result` |
| execute | `dynamic_tool.registered` | `dynamic_tool.executed` |
| verify | `dynamic_tool.executed` | `verification.result` |

Each end step pairs with the latest unpaired start step before it in the attempt. A call
that raised (`agent.loop_error`) emits no end step and is not measured. Budgets live in
`harness/metrics/thresholds.json` (`step_latency_budgets_seconds`).
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from harness.mappings.runtime import RuntimeEvent, workflow_steps
from harness.sensors.kpi_thresholds import THRESHOLDS_PATH

STEP_SPANS: dict[str, tuple[str, str]] = {
    "why": ("rag.symptom", "llm.why"),
    "how": ("rag.repair", "llm.how"),
    "jev": ("jev.requested", "jev.result"),
    "execute": ("dynamic_tool.registered", "dynamic_tool.executed"),
    "verify": ("dynamic_tool.executed", "verification.result"),
}


def load_step_budgets(path: Path = THRESHOLDS_PATH) -> dict[str, float]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    budgets = raw.get("step_latency_budgets_seconds", {})
    return {str(key): float(value) for key, value in budgets.items()}


def step_durations(events: Iterable[RuntimeEvent]) -> dict[str, list[float]]:
    timelines: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for step in workflow_steps(events):
        incident_id = step.get("incident_id")
        attempt = step.get("attempt")
        ts = step.get("ts")
        if (
            isinstance(incident_id, str)
            and isinstance(attempt, int)
            and isinstance(ts, int | float)
            and not isinstance(ts, bool)
        ):
            timelines.setdefault((incident_id, attempt), []).append(step)

    durations: dict[str, list[float]] = {name: [] for name in STEP_SPANS}
    for timeline in timelines.values():
        open_starts: dict[str, float] = {}
        for step in sorted(timeline, key=lambda row: float(row["ts"])):
            name = step.get("step")
            ts = float(step["ts"])
            # End before start: `dynamic_tool.executed` ends `execute` and starts `verify`.
            for span, (_start, end) in STEP_SPANS.items():
                if name == end and span in open_starts:
                    durations[span].append(ts - open_starts.pop(span))
            for span, (start, _end) in STEP_SPANS.items():
                if name == start:
                    open_starts[span] = ts
    return durations


def _p90(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.9 * len(ordered)) - 1]  # nearest rank, as in the KPIs


def step_latency_budget(
    events: Iterable[RuntimeEvent],
    *,
    budgets: Mapping[str, float],
    stage: str = "continuous",
    now: float | None = None,
) -> dict[str, Any]:
    durations = step_durations(events)
    violations: list[dict[str, Any]] = []
    checked = 0
    for span, values in durations.items():
        if not values or span not in budgets:
            continue
        checked += 1
        p90 = _p90(values)
        if p90 > budgets[span]:
            violations.append(
                {
                    "incident_id": None,
                    "detail": (
                        f"{span} p90={p90:.2f}s exceeds budget {budgets[span]:g}s "
                        f"(n={len(values)})"
                    ),
                }
            )

    if checked == 0:
        status, severity, message = "skipped", "info", "no measured steps in the window"
    elif violations:
        status, severity = "fail", "error"
        message = f"{len(violations)} of {checked} steps over their p90 latency budget"
    else:
        status, severity = "pass", "info"
        message = f"{checked} steps within their p90 latency budget"
    return {
        "schema_version": "v1",
        "sensor_name": "step_latency_budget",
        "lane": "behavior",
        "sensor_type": "computational",
        "stage": stage,
        "status": status,
        "severity": severity,
        "message": message,
        "violations": violations,
        "checked": checked,
        "ts": time.time() if now is None else now,
    }
