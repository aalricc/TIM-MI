#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def wilson_ci(
    successes: int,
    total: int,
    z: float = 1.959963984540054,
) -> tuple[float, float, float]:
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


def _sum_metric(
    *,
    breakdown: dict[str, dict[str, int | float]],
    success_key: str,
    total_key: str,
) -> tuple[int, int]:
    successes = 0
    totals = 0
    for scenario in breakdown.values():
        successes += int(scenario.get(success_key, 0))
        totals += int(scenario.get(total_key, 0))
    return successes, totals


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarize full_loop_outcome_compare with Wilson CIs"
    )
    parser.add_argument(
        "--report",
        type=Path,
        required=True,
        help="Path to full_loop_outcome_compare JSON",
    )
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
        if "arm" in item:
            label = str(item.get("arm"))
        elif "readonly_replan_enabled" in item:
            enabled = bool(item.get("readonly_replan_enabled", False))
            label = "flag ON" if enabled else "flag OFF"
        else:
            label = "single"
        aggregate = item.get("aggregate", {})
        breakdown = aggregate.get("scenario_breakdown", {})

        strict_runs, total_runs = _sum_metric(
            breakdown=breakdown,
            success_key="strict_success_runs",
            total_key="total_runs",
        )

        point, low, high = wilson_ci(strict_runs, total_runs)
        print(
            f"\n[{label}] aggregate: {strict_runs}/{total_runs}  "
            f"rate={_format_pct(point)}  CI95=[{_format_pct(low)}, {_format_pct(high)}]"
        )
        print("scenario  success/total    rate       CI95")
        print("--------  -------------  --------  -----------------------")

        for scenario_id in sorted(breakdown):
            row = breakdown[scenario_id]
            success = int(row.get("strict_success_runs", 0))
            total = int(row.get("total_runs", 0))
            sp, sl, sh = wilson_ci(success, total)
            print(
                f"{scenario_id:<8}  {success:>3}/{total:<9}  {_format_pct(sp)}  "
                f"[{_format_pct(sl)}, {_format_pct(sh)}]"
            )

        print("\nGAVE-UP / UNRESOLVED RATE")
        unresolved_runs, unresolved_total = _sum_metric(
            breakdown=breakdown,
            success_key="gave_up_or_unresolved_runs",
            total_key="total_runs",
        )
        point, low, high = wilson_ci(unresolved_runs, unresolved_total)
        print(
            f"[{label}] aggregate: {unresolved_runs}/{unresolved_total}  "
            f"rate={_format_pct(point)}  CI95=[{_format_pct(low)}, {_format_pct(high)}]"
        )

        print("\nCORRECT ACTION ANYWHERE IN PLAN (Wilson 95% CI)")
        in_candidates_runs, in_candidates_total = _sum_metric(
            breakdown=breakdown,
            success_key="correct_action_anywhere_in_plan_runs",
            total_key="total_runs",
        )
        point, low, high = wilson_ci(in_candidates_runs, in_candidates_total)
        print(
            f"[{label}] aggregate: {in_candidates_runs}/{in_candidates_total}  "
            f"rate={_format_pct(point)}  CI95=[{_format_pct(low)}, {_format_pct(high)}]"
        )
        print("scenario  anywhere/total        rate       CI95")
        print("--------  -------------------  --------  -----------------------")
        for scenario_id in sorted(breakdown):
            row = breakdown[scenario_id]
            success = int(row.get("correct_action_anywhere_in_plan_runs", 0))
            total = int(row.get("total_runs", 0))
            sp, sl, sh = wilson_ci(success, total)
            print(
                f"{scenario_id:<8}  {success:>3}/{total:<16}  {_format_pct(sp)}  "
                f"[{_format_pct(sl)}, {_format_pct(sh)}]"
            )

        print("\nCORRECT ACTION IN STEP-1 CANDIDATES (selector input)")
        step1_runs, step1_total = _sum_metric(
            breakdown=breakdown,
            success_key="correct_action_in_step1_candidates_runs",
            total_key="total_runs",
        )
        point, low, high = wilson_ci(step1_runs, step1_total)
        print(
            f"[{label}] aggregate: {step1_runs}/{step1_total}  "
            f"rate={_format_pct(point)}  CI95=[{_format_pct(low)}, {_format_pct(high)}]"
        )
        print("scenario  step1/total           rate       CI95")
        print("--------  -------------------  --------  -----------------------")
        for scenario_id in sorted(breakdown):
            row = breakdown[scenario_id]
            success = int(row.get("correct_action_in_step1_candidates_runs", 0))
            total = int(row.get("total_runs", 0))
            sp, sl, sh = wilson_ci(success, total)
            print(
                f"{scenario_id:<8}  {success:>3}/{total:<16}  {_format_pct(sp)}  "
                f"[{_format_pct(sl)}, {_format_pct(sh)}]"
            )

        print("\nSTRICT SUCCESS | CORRECT-IN-CANDIDATES (Wilson 95% CI)")
        conditional_runs, conditional_total = _sum_metric(
            breakdown=breakdown,
            success_key="strict_success_given_correct_candidates_runs",
            total_key="strict_success_given_correct_candidates_denominator",
        )
        point, low, high = wilson_ci(conditional_runs, conditional_total)
        print(
            f"[{label}] aggregate: {conditional_runs}/{conditional_total}  "
            f"rate={_format_pct(point)}  CI95=[{_format_pct(low)}, {_format_pct(high)}]"
        )
        print("scenario  strict|in-candidates  rate       CI95")
        print("--------  --------------------  --------  -----------------------")
        for scenario_id in sorted(breakdown):
            row = breakdown[scenario_id]
            success = int(row.get("strict_success_given_correct_candidates_runs", 0))
            total = int(row.get("strict_success_given_correct_candidates_denominator", 0))
            sp, sl, sh = wilson_ci(success, total)
            print(
                f"{scenario_id:<8}  {success:>3}/{total:<17}  {_format_pct(sp)}  "
                f"[{_format_pct(sl)}, {_format_pct(sh)}]"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
