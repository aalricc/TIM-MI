"""Behaviour sensors: LLM token and JEV call budgets per incident over a log window.

Both read the per-incident KPIs (`llm_tokens_per_incident` from the `llm_usage_total` on
`llm.why` / `llm.how` steps; `jev_calls_per_incident` from `jev.requested` steps) and
compare the p90 with `harness/metrics/thresholds.json`. A budget of `null` makes the
sensor report the p90 without failing.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from typing import Any

from harness.mappings.runtime import RuntimeEvent
from harness.metrics.kpis import compute_kpis


def _budget_result(
    *,
    name: str,
    summary: Mapping[str, Any],
    unit: str,
    budget: float | None,
    stage: str,
    now: float | None,
) -> dict[str, Any]:
    count = int(summary["n"])
    p90 = summary["p90"]
    violations: list[dict[str, Any]] = []
    if count == 0:
        status, message = "skipped", f"no incidents with {unit} in the window"
    elif budget is None:
        status = "pass"
        message = f"p90 {p90:g} {unit} per incident over {count} incidents (no budget set)"
    elif p90 > budget:
        status = "fail"
        message = f"p90 {p90:g} {unit} per incident exceeds budget {budget:g}"
        violations.append(
            {
                "incident_id": None,
                "detail": f"{unit} p90={p90:g} exceeds budget {budget:g} (n={count})",
            }
        )
    else:
        status = "pass"
        message = f"p90 {p90:g} {unit} per incident within budget {budget:g} ({count} incidents)"
    return {
        "schema_version": "v1",
        "sensor_name": name,
        "lane": "behavior",
        "sensor_type": "computational",
        "stage": stage,
        "status": status,
        "severity": "error" if status == "fail" else "info",
        "message": message,
        "violations": violations,
        "checked": count,
        "ts": time.time() if now is None else now,
    }


def call_budgets(
    events: Iterable[RuntimeEvent],
    *,
    thresholds: Mapping[str, float],
    stage: str = "continuous",
    now: float | None = None,
) -> list[dict[str, Any]]:
    """`token_budget` and `call_rate_budget` sensor results, in that order."""
    kpis = compute_kpis(events)
    return [
        _budget_result(
            name="token_budget",
            summary=kpis["llm_tokens_per_incident"],
            unit="tokens",
            budget=thresholds.get("max_tokens_per_incident_p90"),
            stage=stage,
            now=now,
        ),
        _budget_result(
            name="call_rate_budget",
            summary=kpis["jev_calls_per_incident"],
            unit="JEV calls",
            budget=thresholds.get("max_jev_calls_per_incident_p90"),
            stage=stage,
            now=now,
        ),
    ]
