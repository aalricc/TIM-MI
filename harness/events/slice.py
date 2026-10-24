"""Slice runtime JSONL event logs by time window and incident id.

Usage:
    python -m harness.events.slice [--since <ts>] [--until <ts>] [--incident <id>] <events.jsonl>
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from harness.mappings.runtime import RuntimeEvent, load_event_log


def _parse_timestamp(value: str) -> float:
    text = value.strip()
    try:
        number = float(text)
    except ValueError:
        number = None
    if number is not None:
        if not math.isfinite(number):
            raise ValueError(f"invalid timestamp: {value!r}")
        return number

    iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    parsed = datetime.fromisoformat(iso_text)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include an offset: {value!r}")
    return parsed.timestamp()


def _incident_id(event: RuntimeEvent) -> str | None:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    value = payload.get("incident_id")
    return value if isinstance(value, str) and value != "" else None


def _ts(event: RuntimeEvent) -> float | None:
    value = event.get("ts")
    if isinstance(value, int | float) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number):
            return number
    return None


def slice_events(
    events: Iterable[RuntimeEvent],
    *,
    since: float | None = None,
    until: float | None = None,
    incident: str | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        if incident is not None and _incident_id(event) != incident:
            continue
        ts = _ts(event)
        if since is not None and (ts is None or ts < since):
            continue
        if until is not None and (ts is None or ts > until):
            continue
        if isinstance(event, Mapping):
            rows.append(dict(event))
    return rows


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m harness.events.slice",
        description="Print matching JSONL event rows to stdout.",
    )
    parser.add_argument("log_path", help="Path to events.jsonl")
    parser.add_argument("--since", help="Inclusive lower bound: epoch seconds or ISO-8601")
    parser.add_argument("--until", help="Inclusive upper bound: epoch seconds or ISO-8601")
    parser.add_argument("--incident", help="Incident id to keep")
    return parser


def main(argv: list[str]) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    try:
        since = _parse_timestamp(args.since) if args.since is not None else None
        until = _parse_timestamp(args.until) if args.until is not None else None
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if since is not None and until is not None and since > until:
        print("--since must be <= --until", file=sys.stderr)
        return 2

    try:
        events = load_event_log(Path(args.log_path))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    for row in slice_events(events, since=since, until=until, incident=args.incident):
        print(json.dumps(row, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
