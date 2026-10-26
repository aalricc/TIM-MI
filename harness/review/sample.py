"""Build markdown packets for weekly human review of decision logs.

Usage:
    python -m harness.review.sample --n 20
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from harness.mappings.runtime import (
    RuntimeEvent,
    decision_traces,
    incident_signatures,
    load_event_log,
)


def _payload(event: RuntimeEvent) -> Mapping[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _incident_id(event: RuntimeEvent) -> str | None:
    value = _payload(event).get("incident_id")
    return value if isinstance(value, str) and value != "" else None


def _number(value: Any) -> float | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


# Copied from `incident.recovered` so the reviewer can weigh the data an action lost.
RECOVERY_FIELDS = (
    "source",
    "action_key",
    "widget_rows_before",
    "widget_rows_after",
    "rows_lost",
    "reward",
)


def review_verdict_template(incident_id: str) -> dict[str, Any]:
    """Blank verdict: the `null` judgements fail `review_verdict` v1 until filled in, so
    an unedited template is never mistaken for a negative verdict."""
    return {
        "schema_version": "v1",
        "incident_id": incident_id,
        "diagnosis_correct": None,
        "action_appropriate": None,
        "notes": "",
    }


def _outcomes(events: Iterable[RuntimeEvent]) -> dict[str, dict[str, Any]]:
    by_incident: dict[str, dict[str, Any]] = {}

    def ensure(incident_id: str) -> dict[str, Any]:
        if incident_id not in by_incident:
            by_incident[incident_id] = {
                "incident_id": incident_id,
                "status": "open",
                "started_at": None,
                "ended_at": None,
                "ttr_seconds": None,
                "resolution": None,
                "attempts": None,
            }
        return by_incident[incident_id]

    for event in events:
        incident_id = _incident_id(event)
        if incident_id is None:
            continue
        outcome = ensure(incident_id)
        event_type = event.get("event_type")
        payload = _payload(event)
        ts = _number(event.get("ts"))

        if event_type == "incident.started" and outcome["started_at"] is None:
            outcome["started_at"] = ts
            continue
        if event_type == "workflow.step":
            attempt = payload.get("attempt")
            if isinstance(attempt, int) and not isinstance(attempt, bool):
                current = outcome["attempts"]
                if not isinstance(current, int) or attempt > current:
                    outcome["attempts"] = attempt
            continue
        if event_type == "incident.recovered":
            outcome["status"] = "recovered"
            outcome["ended_at"] = ts
            outcome["resolution"] = "incident_recovered"
            attempts = payload.get("attempts")
            if isinstance(attempts, int) and not isinstance(attempts, bool):
                outcome["attempts"] = attempts
            for field in RECOVERY_FIELDS:
                if field in payload:
                    outcome[field] = payload[field]
            continue
        if event_type == "incident.recovery_failed":
            outcome["status"] = "failed"
            outcome["ended_at"] = ts
            outcome["resolution"] = payload.get("reason")
            continue
        if event_type == "incident.closed" and outcome["status"] != "recovered":
            outcome["status"] = "closed"
            outcome["ended_at"] = ts
            outcome["resolution"] = payload.get("resolution")

    for outcome in by_incident.values():
        started_at = outcome["started_at"]
        ended_at = outcome["ended_at"]
        if isinstance(started_at, float) and isinstance(ended_at, float):
            outcome["ttr_seconds"] = max(0.0, ended_at - started_at)
    return by_incident


def sample_packets(events: Iterable[RuntimeEvent], *, n: int) -> list[dict[str, Any]]:
    if n <= 0:
        raise ValueError("n must be >= 1")

    rows = [dict(event) for event in events if isinstance(event, Mapping)]
    traces = decision_traces(rows)
    traces_by_incident: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trace in traces:
        incident_id = trace.get("incident_id")
        if isinstance(incident_id, str) and incident_id != "":
            traces_by_incident[incident_id].append(trace)

    signatures_by_incident = {
        str(record["incident_id"]): record
        for record in incident_signatures(rows)
        if isinstance(record.get("incident_id"), str) and record["incident_id"] != ""
    }
    outcomes = _outcomes(rows)

    def sort_key(incident_id: str) -> tuple[float, str]:
        signature = signatures_by_incident.get(incident_id, {})
        started_at = _number(signature.get("started_at"))
        if started_at is None:
            started_at = _number(outcomes.get(incident_id, {}).get("started_at"))
        return (started_at if started_at is not None else float("-inf"), incident_id)

    incident_ids = sorted(traces_by_incident, key=sort_key, reverse=True)[:n]

    packets: list[dict[str, Any]] = []
    for incident_id in incident_ids:
        traces_for_incident = traces_by_incident[incident_id]
        selected_operations = sorted(
            {
                str(selected.get("operation"))
                for trace in traces_for_incident
                if isinstance((selected := trace.get("selected_candidate")), Mapping)
                and isinstance(selected.get("operation"), str)
                and selected.get("operation") != ""
            }
        )
        outcome = dict(outcomes.get(incident_id, {"incident_id": incident_id, "status": "open"}))
        if selected_operations:
            outcome["selected_operations"] = selected_operations

        packets.append(
            {
                "incident_id": incident_id,
                "incident_signature": signatures_by_incident.get(
                    incident_id,
                    {"schema_version": "v1", "incident_id": incident_id},
                ),
                "decision_traces": traces_for_incident,
                "outcome": outcome,
            }
        )

    return packets


def _json_block(value: Any) -> str:
    return "```json\n" + json.dumps(value, indent=2, sort_keys=True) + "\n```"


def packet_markdown(packet: Mapping[str, Any]) -> str:
    incident_id = packet.get("incident_id")
    if not isinstance(incident_id, str) or incident_id == "":
        raise ValueError("packet incident_id is required")

    sections = [
        f"# Review Packet: {incident_id}",
        "",
        "Fill in the verdict (`true`/`false` for both judgements) and save it as JSON",
        "under `results/reviews/`.",
        "",
        "## Incident Signature",
        _json_block(packet.get("incident_signature")),
        "",
        "## Decision Traces",
        _json_block(packet.get("decision_traces")),
        "",
        "## Outcome Summary",
        _json_block(packet.get("outcome")),
        "",
        "## Reviewer Verdict (`review_verdict.schema.v1.json`)",
        _json_block(review_verdict_template(incident_id)),
        "",
    ]
    return "\n".join(sections)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m harness.review.sample",
        description="Print markdown decision-review packets to stdout.",
    )
    parser.add_argument("--n", type=int, default=20, help="Number of incidents to sample")
    parser.add_argument(
        "--events",
        type=Path,
        default=Path("state/events.jsonl"),
        help="Path to runtime event log (default: state/events.jsonl)",
    )
    return parser


def main(argv: list[str]) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if args.n < 1:
        print("--n must be >= 1", file=sys.stderr)
        return 2

    try:
        events = load_event_log(args.events)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    packets = sample_packets(events, n=args.n)
    if not packets:
        print("no incidents with decision traces found", file=sys.stderr)
        return 1

    for index, packet in enumerate(packets):
        if index:
            print("\n---\n")
        print(packet_markdown(packet), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

