"""Behaviour sensor: recovery KPIs over a log window checked against thresholds.

Thresholds live in `harness/metrics/thresholds.json`. A KPI that is undefined for the
window (no incidents, no recoveries, no JEV results) is not checked; with nothing to
check the sensor reports `skipped`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from harness.mappings.runtime import RuntimeEvent
from harness.metrics.kpis import compute_kpis

THRESHOLDS_PATH = Path(__file__).resolve().parents[1] / "metrics" / "thresholds.json"


def load_thresholds(path: Path = THRESHOLDS_PATH) -> dict[str, float]:
    """The scalar thresholds. `null` means "report only" and is left out; nested tables
    (step budgets) have their own loader."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(key): float(value)
        for key, value in raw.items()
        if value is not None and not isinstance(value, dict)
    }


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def kpi_thresholds(
    events: Iterable[RuntimeEvent],
    *,
    thresholds: Mapping[str, float],
    stage: str = "continuous",
    now: float | None = None,
) -> dict[str, Any]:
    kpis = compute_kpis(events)
    recovery_rate = kpis["recovery_rate"]
    ttr_p90 = kpis["time_to_recover_seconds"]["p90"]
    request_error_rate = kpis["decision"]["jev_request_error_rate"]

    # (KPI name, value, threshold key, True when the threshold is a minimum)
    checks = (
        ("recovery_rate", recovery_rate, "min_recovery_rate", True),
        ("ttr_p90_seconds", ttr_p90, "max_ttr_p90_seconds", False),
        ("jev_request_error_rate", request_error_rate, "max_request_error_rate", False),
    )
    violations: list[dict[str, Any]] = []
    checked = 0
    for name, value, threshold_key, is_minimum in checks:
        if not _is_number(value) or threshold_key not in thresholds:
            continue
        checked += 1
        threshold = thresholds[threshold_key]
        breached = value < threshold if is_minimum else value > threshold
        if breached:
            violations.append(
                {
                    "incident_id": None,
                    "detail": f"{name}={value:.4g} breaches {threshold_key}={threshold:g}",
                }
            )

    started = kpis["incidents"]["started"]
    if checked == 0:
        status, severity = "skipped", "info"
        message = f"no KPI defined over the window ({started} incidents)"
    elif violations:
        status, severity = "fail", "error"
        message = f"{len(violations)} of {checked} KPI thresholds breached over {started} incidents"
    else:
        status, severity = "pass", "info"
        message = f"{checked} KPI thresholds met over {started} incidents"
    return {
        "schema_version": "v1",
        "sensor_name": "kpi_thresholds",
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
