"""Deterministic read-model mappings: runtime outputs -> harness contract records.

Inputs are the runtime's own artifacts:
- event log rows `{"ts", "event_type", "payload"}` (EventLog / `state/events.jsonl`)
- `state/class_store.json` (ClassMemory)
- `state/skills.md` (ClassMemory.upsert_skills_section / RecoveryAgent._append_skill_entry)

Mappings never coerce or repair runtime values: fields are passed through as emitted so
that contract validation surfaces payload drift instead of hiding it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "v1"

# Runtime `workflow.step` name -> README.md loop step (1-16).
README_STEP: dict[str, int | None] = {
    "monitor.unhealthy_detected": 1,
    "diagnostics.collected": 2,
    "rag.symptom": 3,
    "llm.why": 4,
    "context.reused": 5,
    "class_memory.lookup": 5,
    "path.selected": 5,
    "rag.repair": 6,
    "llm.how": 7,
    "jev.requested": 8,
    "jev.result": 8,
    "path.fallback": 8,
    "security.gate": 9,
    "dynamic_tool.registered": 10,
    "dynamic_tool.executed": 11,
    "verification.result": 12,
    "persist.tool_catalog": 13,
    "persist.policy_store": 14,
    "persist.skills": 15,
    "persist.class_memory": 16,
    "post_recovery.ping": None,
    "attempt.result": None,
}

RuntimeEvent = Mapping[str, Any]


def load_event_log(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() == "":
            continue
        record = json.loads(line)
        if isinstance(record, dict):
            rows.append(record)
    return rows


def _payload(event: RuntimeEvent) -> Mapping[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _incident_id(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("incident_id")
    return value if isinstance(value, str) and value != "" else None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --------------------------------------------------------------------------- workflow_step


def workflow_step_record(event: RuntimeEvent) -> dict[str, Any]:
    if event.get("event_type") != "workflow.step":
        raise ValueError(f"not a workflow.step event: {event.get('event_type')!r}")
    payload = _payload(event)
    step = payload.get("step")
    return {
        **payload,
        "schema_version": SCHEMA_VERSION,
        "ts": event.get("ts"),
        "readme_step": README_STEP.get(step) if isinstance(step, str) else None,
    }


def workflow_steps(events: Iterable[RuntimeEvent]) -> list[dict[str, Any]]:
    return [workflow_step_record(e) for e in events if e.get("event_type") == "workflow.step"]


# --------------------------------------------------------------------------- decision_trace


def _candidate_rows(value: Any) -> list[dict[str, Any]]:
    # Non-dict items are EventLog truncation markers ("<truncated:N items>").
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        row: dict[str, Any] = {
            "name": item.get("name"),
            "operation": item.get("operation"),
            "params": item.get("params"),
        }
        if "rationale" in item:
            row["rationale"] = item.get("rationale")
        rows.append(row)
    return rows


def _source_from_path_mode(path_mode: Any) -> str | None:
    if path_mode == "fast_path":
        return "class_memory"
    if path_mode == "full_loop":
        return "full_loop"
    return None


def decision_traces(events: Iterable[RuntimeEvent]) -> list[dict[str, Any]]:
    """One record per action-selection outcome, in log order.

    Context (attempt, source, evaluated_count) comes from the most recent
    incident-scoped workflow.step events preceding the decision event.
    """
    attempt_by_incident: dict[str, int] = {}
    source_by_incident: dict[str, str | None] = {}
    evaluated_by_incident: dict[str, int | None] = {}
    selected_by_incident: dict[str, Mapping[str, Any]] = {}
    traces: list[dict[str, Any]] = []

    for event in events:
        event_type = event.get("event_type")
        payload = _payload(event)
        incident_id = _incident_id(payload)
        if incident_id is None:
            continue

        if event_type == "workflow.step":
            attempt = payload.get("attempt")
            if isinstance(attempt, int) and not isinstance(attempt, bool):
                attempt_by_incident[incident_id] = attempt
            step = payload.get("step")
            if step == "path.selected":
                source_by_incident[incident_id] = _source_from_path_mode(payload.get("path_mode"))
                evaluated_by_incident[incident_id] = None
            elif step == "jev.requested":
                source = payload.get("source")
                if source in {"class_memory", "full_loop"}:
                    source_by_incident[incident_id] = source
                count = payload.get("candidate_count")
                evaluated_by_incident[incident_id] = count if _is_int(count) else None
            continue

        base: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "ts": event.get("ts"),
            "incident_id": incident_id,
            "attempt": attempt_by_incident.get(incident_id),
        }

        if event_type == "decision.selected":
            selected_by_incident[incident_id] = payload
        elif event_type == "planning.how":
            selected = selected_by_incident.pop(incident_id, None)
            decision = selected.get("decision") if selected is not None else None
            if not isinstance(decision, Mapping):
                decision = payload.get("jev_decision")
            decision_map: Mapping[str, Any] = decision if isinstance(decision, Mapping) else {}
            if selected is not None:
                name = selected.get("selected_candidate")
                operation = selected.get("selected_operation")
            else:
                name = decision_map.get("selected_candidate_name")
                operation = decision_map.get("selected_candidate_operation")
            source = payload.get("source")
            if source not in {"class_memory", "full_loop"}:
                source = source_by_incident.get(incident_id)
            traces.append(
                {
                    **base,
                    "source": source,
                    "outcome": "selected",
                    "reason": decision_map.get("reason"),
                    "candidates_origin": "how_proposal",
                    "candidates": _candidate_rows(
                        [payload.get("primary"), *(payload.get("alternatives") or [])]
                    ),
                    "evaluated_count": evaluated_by_incident.get(incident_id),
                    "selected_candidate": {
                        "name": name,
                        "operation": operation,
                        "executed_candidate": payload.get("executed_candidate"),
                        "executed_tool_name": payload.get("executed_tool_name"),
                    },
                    "decision": dict(decision_map),
                    "action_key": payload.get("action_key"),
                    "class_id": payload.get("class_id"),
                    "source_event": "planning.how",
                }
            )
        elif event_type == "decision.unusable":
            candidates = _candidate_rows(payload.get("candidate_actions"))
            decision = payload.get("decision")
            traces.append(
                {
                    **base,
                    "source": source_by_incident.get(incident_id),
                    "outcome": "unusable",
                    "reason": payload.get("reason"),
                    "candidates_origin": "jev_evaluated",
                    "candidates": candidates,
                    "evaluated_count": len(candidates),
                    "selected_candidate": None,
                    "decision": dict(decision) if isinstance(decision, Mapping) else decision,
                    "action_key": payload.get("selected_action_key"),
                    "class_id": None,
                    "source_event": "decision.unusable",
                }
            )
        elif event_type == "class_memory.fast_path_skipped":
            decision = payload.get("decision")
            count = payload.get("candidate_count")
            traces.append(
                {
                    **base,
                    "source": "class_memory",
                    "outcome": "fast_path_fallback",
                    "reason": payload.get("reason"),
                    "candidates_origin": "not_emitted",
                    "candidates": [],
                    "evaluated_count": count if _is_int(count) else None,
                    "selected_candidate": None,
                    "decision": dict(decision) if isinstance(decision, Mapping) else decision,
                    "action_key": payload.get("selected_action_key"),
                    "class_id": payload.get("class_id"),
                    "source_event": "class_memory.fast_path_skipped",
                }
            )

    return traces


# --------------------------------------------------------------------------- incident_signature


def incident_signatures(events: Iterable[RuntimeEvent]) -> list[dict[str, Any]]:
    """One record per incident, ordered by first appearance in the log."""
    records: dict[str, dict[str, Any]] = {}
    seen_diagnosis: set[str] = set()
    seen_class: set[str] = set()

    def record_for(incident_id: str) -> dict[str, Any]:
        if incident_id not in records:
            records[incident_id] = {
                "schema_version": SCHEMA_VERSION,
                "incident_id": incident_id,
                "started_at": None,
                "monitor_detail": None,
                "failure_type": None,
                "hypotheses": [],
                "evidence": [],
                "class_id": None,
                "class_match": None,
            }
        return records[incident_id]

    for event in events:
        event_type = event.get("event_type")
        payload = _payload(event)
        incident_id = _incident_id(payload)
        if incident_id is None:
            continue
        if event_type == "incident.started":
            record = record_for(incident_id)
            if record["started_at"] is None:
                record["started_at"] = event.get("ts")
        elif (
            event_type == "workflow.step"
            and payload.get("step") == "monitor.unhealthy_detected"
        ):
            record_for(incident_id)["monitor_detail"] = payload.get("detail")
        elif event_type == "diagnosis.why" and incident_id not in seen_diagnosis:
            seen_diagnosis.add(incident_id)
            record = record_for(incident_id)
            record["failure_type"] = payload.get("failure_type")
            record["hypotheses"] = payload.get("hypotheses")
            record["evidence"] = payload.get("evidence")
        elif event_type == "class_memory.decision" and incident_id not in seen_class:
            seen_class.add(incident_id)
            record = record_for(incident_id)
            record["class_id"] = payload.get("class_id")
            record["class_match"] = {
                "s1": payload.get("s1"),
                "s2": payload.get("s2"),
                "margin": payload.get("margin"),
                "new_class": payload.get("new_class"),
                "fast_path_eligible": payload.get("fast_path_eligible"),
                "first_failing_gate_reason": payload.get("first_failing_gate_reason"),
            }

    return list(records.values())


# --------------------------------------------------------------------------- class store


def _classes(store: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    classes = store.get("classes")
    if not isinstance(classes, Mapping):
        return []
    return [
        (class_id, record)
        for class_id, record in sorted(classes.items())
        if isinstance(class_id, str) and isinstance(record, Mapping)
    ]


def problem_classes(store: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for class_id, record in _classes(store):
        centroid = record.get("centroid")
        actions = record.get("actions")
        records.append(
            {
                "schema_version": SCHEMA_VERSION,
                "store_schema_version": record.get("schema_version"),
                "class_id": class_id,
                "created_ts": record.get("created_ts"),
                "updated_ts": record.get("updated_ts"),
                "n_incidents": record.get("n_incidents"),
                "templates": record.get("templates"),
                "centroid_dim": len(centroid) if isinstance(centroid, list) else 0,
                "action_keys": sorted(actions) if isinstance(actions, Mapping) else [],
                "skill_anchor": record.get("skill_anchor"),
            }
        )
    return records


def _beta_count(value: Any) -> Any:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value) - 1.0
    return None


def class_action_stats(store: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for class_id, record in _classes(store):
        actions = record.get("actions")
        if not isinstance(actions, Mapping):
            continue
        for action_key, stats in sorted(actions.items()):
            if not isinstance(stats, Mapping):
                continue
            template = stats.get("param_template")
            template_map: Mapping[str, Any] = template if isinstance(template, Mapping) else {}
            records.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "class_id": class_id,
                    "action_key": action_key,
                    "action_name": template_map.get("name"),
                    "operation": template_map.get("operation"),
                    "params_template": template_map.get("params"),
                    "source": stats.get("source"),
                    "attempts": stats.get("attempts"),
                    "alpha": stats.get("alpha"),
                    "beta": stats.get("beta"),
                    "successes": _beta_count(stats.get("alpha")),
                    "failures": _beta_count(stats.get("beta")),
                    "mean_latency_ms": stats.get("mean_latency"),
                    "last_ok_ts": stats.get("last_ok_ts"),
                    "last_failure_ts": stats.get("last_failure_ts"),
                    "cooldown_until_ts": stats.get("cooldown_until_ts"),
                }
            )
    return records


# --------------------------------------------------------------------------- skills.md

_CLASS_SECTION_RE = re.compile(
    r"<!-- class:(?P<class_id>[^\s>]+) begin -->\n"
    r"(?P<body>.*?)\n"
    r"<!-- class:(?P=class_id) end -->",
    re.DOTALL,
)
_LEGACY_BLOCK_RE = re.compile(
    r"^---\nname: (?P<name>[^\n]*)\n(?P<front>.*?)^---\n(?P<body>.*?)(?=^---\nname: |\Z)",
    re.DOTALL | re.MULTILINE,
)
_BULLET_RE = re.compile(r"^- (?P<key>[a-z_]+): (?P<value>.*?)\s*$", re.MULTILINE)
_CHAMPION_HEADER = "\n## Champion Template\n"


def _bullets(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for match in _BULLET_RE.finditer(text):
        values.setdefault(match.group("key"), match.group("value"))
    return values


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _csv_or_none(value: str | None) -> list[str] | None:
    if value is None:
        return None
    if value == "none":
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _class_section_entry(class_id: str, body: str) -> dict[str, Any]:
    head, _, template_text = body.partition(_CHAMPION_HEADER)
    bullets = _bullets(head)
    try:
        champion_template: Any = json.loads(template_text)
    except json.JSONDecodeError:
        champion_template = None
    last_verified = bullets.get("last_verified_ts")
    return {
        "schema_version": SCHEMA_VERSION,
        "format": "class_section",
        "skill_id": f"class:{class_id}",
        "class_id": class_id,
        "champion_action_key": bullets.get("champion_action_key"),
        "champion_lcb": _to_float(bullets.get("champion_lcb")),
        "attempts": _to_int(bullets.get("attempts")),
        "mean_latency_ms": _to_float(bullets.get("mean_latency_ms")),
        "last_verified_ts": None if last_verified == "None" else _to_float(last_verified),
        "runner_ups": _csv_or_none(bullets.get("runner_ups")),
        "templates": _csv_or_none(bullets.get("templates")),
        "champion_template": champion_template,
    }


def _legacy_block_entry(name: str, front: str, body: str) -> dict[str, Any]:
    version = re.search(r"^version: (\S+)\s*$", front, re.MULTILINE)
    tools = re.search(r"^tools:\n  - (\S+)\s*$", front, re.MULTILINE)
    tool_name = re.search(r"^# Skill: (.*?)\s*$", body, re.MULTILINE)
    bullets = _bullets(body)
    return {
        "schema_version": SCHEMA_VERSION,
        "format": "legacy_block",
        "skill_id": name.strip(),
        "tool_name": tool_name.group(1) if tool_name else None,
        "operation": tools.group(1) if tools else None,
        "failure_type": bullets.get("failure_type"),
        "version": version.group(1) if version else None,
        "learned_at": bullets.get("timestamp"),
        "incident_id": bullets.get("incident_id"),
        "attempts_to_success": _to_int(bullets.get("attempts_to_success")),
        "rl_attempts": _to_float(bullets.get("rl_attempts")),
        "rl_reward_total": _to_float(bullets.get("rl_reward_total")),
        "rl_avg_reward": _to_float(bullets.get("rl_avg_reward")),
    }


def skill_entries(skills_text: str) -> list[dict[str, Any]]:
    """Class sections first (file order), then legacy blocks (file order)."""
    entries: list[dict[str, Any]] = []
    for match in _CLASS_SECTION_RE.finditer(skills_text):
        entries.append(_class_section_entry(match.group("class_id"), match.group("body")))
    remainder = _CLASS_SECTION_RE.sub("", skills_text)
    for match in _LEGACY_BLOCK_RE.finditer(remainder):
        entries.append(
            _legacy_block_entry(match.group("name"), match.group("front"), match.group("body"))
        )
    return entries
