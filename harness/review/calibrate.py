"""Calibrate LLM judge verdicts against human review verdicts.

Usage:
    python -m harness.review.calibrate results/reviews/ <judge-verdicts.jsonl>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from harness.contracts import validate_record


def _load_human_verdicts(path: Path) -> dict[str, dict[str, Any]]:
    verdicts: dict[str, dict[str, Any]] = {}
    for file in sorted(path.glob("*.json")):
        payload = json.loads(file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            continue
        try:
            validate_record(payload, "review_verdict")
        except ValueError:
            continue
        incident_id = payload.get("incident_id")
        if isinstance(incident_id, str) and incident_id != "":
            verdicts[incident_id] = payload
    return verdicts


def _load_judge_verdicts(path: Path) -> dict[str, dict[str, Any]]:
    verdicts: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() == "":
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            continue
        if payload.get("status") != "verdict":
            continue
        try:
            validate_record(payload, "judge_verdict")
        except ValueError:
            continue
        incident_id = payload.get("incident_id")
        if isinstance(incident_id, str) and incident_id != "":
            verdicts[incident_id] = payload
    return verdicts


def calibrate(
    human: dict[str, dict[str, Any]],
    judge: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    fields = ("diagnosis_correct", "action_appropriate")
    matched = sorted(set(human) & set(judge))
    disagreements: list[dict[str, Any]] = []

    agreement: dict[str, float | None] = {}
    for field in fields:
        if not matched:
            agreement[field] = None
            continue
        same = 0
        for incident_id in matched:
            human_value = human[incident_id].get(field)
            judge_value = judge[incident_id].get(field)
            if human_value == judge_value:
                same += 1
            else:
                disagreements.append(
                    {
                        "incident_id": incident_id,
                        "field": field,
                        "human": human_value,
                        "judge": judge_value,
                    }
                )
        agreement[field] = same / len(matched)

    enough = len(matched) >= 20
    diag_ok = agreement["diagnosis_correct"] is not None and agreement["diagnosis_correct"] >= 0.8
    action_ok = (
        agreement["action_appropriate"] is not None and agreement["action_appropriate"] >= 0.8
    )

    return {
        "matched_packets": len(matched),
        "agreement": agreement,
        "disagreements": disagreements,
        "trusted": bool(enough and diag_ok and action_ok),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m harness.review.calibrate",
        description="Compare human and judge verdicts and report calibration trust.",
    )
    parser.add_argument("reviews_dir", type=Path, help="Directory of human review_verdict JSON")
    parser.add_argument("judge_jsonl", type=Path, help="Judge verdict JSONL output")
    return parser


def main(argv: list[str]) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    try:
        human = _load_human_verdicts(args.reviews_dir)
        judge = _load_judge_verdicts(args.judge_jsonl)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(json.dumps(calibrate(human, judge), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
