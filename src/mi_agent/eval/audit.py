from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mi_agent.eval.ope import load_decision_log


def export_audit_trail(
    *,
    decision_log_path: Path,
    event_log_path: Path,
    output_dir: Path,
    run_id: str,
) -> tuple[dict[str, Any], Path, Path]:
    decision_entries = [
        entry.model_dump(mode="json")
        for entry in load_decision_log(decision_log_path)
    ]
    event_entries = _load_event_log(event_log_path)

    action_events = [
        event
        for event in event_entries
        if event.get("event_type")
        in {"agent.action_executed", "agent.skill_executed", "tool.executed"}
    ]
    verifier_events = [
        event
        for event in event_entries
        if event.get("event_type") == "verifier.result"
    ]

    payload: dict[str, Any] = {
        "schema_version": "v1",
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": run_id,
        "counts": {
            "decisions": len(decision_entries),
            "actions": len(action_events),
            "verifier_results": len(verifier_events),
        },
        "decisions": decision_entries,
        "actions": action_events,
        "verifier_results": verifier_events,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{run_id}.json"
    jsonl_path = output_dir / f"{run_id}.jsonl"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for category in ("decisions", "actions", "verifier_results"):
            for item in payload[category]:
                record = {
                    "category": category,
                    "event": item,
                }
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    return payload, json_path, jsonl_path


def _load_event_log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == "":
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events
