#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def wilson_ci(successes: int, total: int, z: float = 1.959963984540054) -> tuple[float, float, float]:
    if total <= 0:
        return 0.0, 0.0, 0.0
    p = successes / total
    denom = 1.0 + (z * z) / total
    center = (p + (z * z) / (2.0 * total)) / denom
    margin = z * math.sqrt((p * (1.0 - p) / total) + ((z * z) / (4.0 * total * total))) / denom
    low = max(0.0, center - margin)
    high = min(1.0, center + margin)
    return p, low, high


def _format_pct(value: float) -> str:
    return f"{value * 100:6.2f}%"


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize full_loop_outcome_compare with Wilson CIs")
    parser.add_argument("--report", type=Path, required=True, help="Path to full_loop_outcome_compare JSON")
    args = parser.parse_args()

    payload = json.loads(args.report.read_text(encoding="utf-8"))
    if payload.get("mode") != "full_loop_outcome_compare":
        raise SystemExit(f"Unexpected report mode: {payload.get('mode')}")

    comparison = payload.get("comparison", [])
    if not isinstance(comparison, list) or not comparison:
        raise SystemExit("Report has no comparison data")

    print(f"report: {args.report}")
    print("\nSTRICT SUCCESS SUMMARY (Wilson 95% CI)")

    for item in comparison:
        enabled = bool(item.get("readonly_replan_enabled", False))
        label = "flag ON" if enabled else "flag OFF"
        aggregate = item.get("aggregate", {})
        breakdown = aggregate.get("scenario_breakdown", {})

        total_runs = 0
        strict_runs = 0
        for scenario in breakdown.values():
            total_runs += int(scenario.get("total_runs", 0))
            strict_runs += int(scenario.get("strict_success_runs", 0))

        point, low, high = wilson_ci(strict_runs, total_runs)
        print(f"\n[{label}] aggregate: {strict_runs}/{total_runs}  rate={_format_pct(point)}  CI95=[{_format_pct(low)}, {_format_pct(high)}]")
        print("scenario  success/total    rate       CI95")
        print("--------  -------------  --------  -----------------------")

        for scenario_id in sorted(breakdown):
            row = breakdown[scenario_id]
            success = int(row.get("strict_success_runs", 0))
            total = int(row.get("total_runs", 0))
            sp, sl, sh = wilson_ci(success, total)
            print(
                f"{scenario_id:<8}  {success:>3}/{total:<9}  {_format_pct(sp)}  [{_format_pct(sl)}, {_format_pct(sh)}]"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
