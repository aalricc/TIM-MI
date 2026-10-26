"""LLM-as-judge for diagnosis and action quality (CP12).

Usage:
    python -m harness.review.judge [--events state/events.jsonl] [--n 20] [--langsmith]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import httpx

from harness.contracts import validate_record
from harness.mappings.runtime import load_event_log
from harness.review.sample import sample_packets

_OPENAI_ENDPOINT = "https://api.openai.com/v1/responses"
_JUDGE_SCHEMA_NAME = "judge_verdict"
_RUNBOOKS = (
    Path("docs/runbooks/drop_widgets_table.md"),
    Path("docs/runbooks/delete_db.md"),
    Path("docs/runbooks/corrupt_db.md"),
)
_FAULT_NAMES = ("drop_widgets_table", "delete_db", "corrupt_db")


def _read_rubric(path: Path = Path("harness/guides/judge_rubric.md")) -> str:
    return path.read_text(encoding="utf-8")


def _extract_approved_actions_sections(paths: Iterable[Path] = _RUNBOOKS) -> dict[str, str]:
    sections: dict[str, str] = {}
    for index, path in enumerate(paths, start=1):
        text = path.read_text(encoding="utf-8")
        match = re.search(
            r"^## Approved recovery actions\n(?P<section>.*?)(^## |\Z)",
            text,
            flags=re.MULTILINE | re.DOTALL,
        )
        if match is None:
            raise ValueError(f"Approved recovery actions section missing: {path}")
        sections[f"runbook_{index}"] = match.group("section").strip()
    return sections


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        text = value
        for name in _FAULT_NAMES:
            text = re.sub(re.escape(name), "[redacted_fault]", text)
        text = re.sub("fault injected", "injected event", text)
        return text
    if isinstance(value, Mapping):
        return {
            str(k): _sanitize_value(v)
            for k, v in value.items()
            if str(k).lower() not in {"fault", "fault_name", "injected_fault"}
        }
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    return value


def _has_selected_action(packet: Mapping[str, Any]) -> bool:
    traces = packet.get("decision_traces")
    if not isinstance(traces, list):
        return False
    for trace in traces:
        if not isinstance(trace, Mapping):
            continue
        if trace.get("outcome") != "selected":
            continue
        selected = trace.get("selected_candidate")
        if isinstance(selected, Mapping) and isinstance(selected.get("operation"), str):
            return selected.get("operation") != ""
    return False


def _judge_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "diagnosis_correct": {"type": "boolean"},
            "action_appropriate": {"type": "boolean"},
            "notes": {"type": "string"},
            "failed_checks": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["D1", "D2", "D3", "A1", "A2", "A3", "A4"],
                },
                "uniqueItems": True,
            },
            "rubric_version": {"type": "string"},
        },
        "required": [
            "diagnosis_correct",
            "action_appropriate",
            "notes",
            "failed_checks",
            "rubric_version",
        ],
        "additionalProperties": False,
    }


def _extract_json_text(payload: Mapping[str, Any]) -> Any:
    output = payload.get("output")
    if not isinstance(output, list):
        raise ValueError("missing output list")
    for item in output:
        if not isinstance(item, Mapping):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for row in content:
            if not isinstance(row, Mapping):
                continue
            if row.get("type") not in {"output_text", "text"}:
                continue
            text = row.get("text")
            if isinstance(text, str):
                return json.loads(text)
    raise ValueError("no JSON text output found")


def _validate_verdict(
    verdict: Mapping[str, Any],
    *,
    incident_id: str,
    model: str,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": "v1",
        "incident_id": incident_id,
        "diagnosis_correct": verdict.get("diagnosis_correct"),
        "action_appropriate": verdict.get("action_appropriate"),
        "notes": verdict.get("notes"),
        "failed_checks": verdict.get("failed_checks", []),
        "rubric_version": verdict.get("rubric_version"),
        "judge_model": model,
        "status": "verdict",
    }
    validate_record(record, "judge_verdict")
    return record


def _skip_verdict(incident_id: str, *, model: str, reason: str) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": "v1",
        "incident_id": incident_id,
        "diagnosis_correct": None,
        "action_appropriate": None,
        "notes": f"skip: {reason}",
        "failed_checks": [],
        "rubric_version": "v1",
        "judge_model": model,
        "status": "skip",
        "skip_reason": reason,
    }
    validate_record(record, "judge_verdict")
    return record


def _judge_packet(
    packet: Mapping[str, Any],
    *,
    model: str,
    endpoint: str,
    api_key: str,
    rubric_text: str,
    approved_actions: Mapping[str, str],
) -> dict[str, Any]:
    incident_id = packet.get("incident_id")
    if not isinstance(incident_id, str) or incident_id == "":
        raise ValueError("packet incident_id missing")

    if not _has_selected_action(packet):
        return _skip_verdict(incident_id, model=model, reason="no_selected_action")

    sanitized = _sanitize_value(packet)

    system_prompt = (
        f"{rubric_text}\n\n"
        "Return JSON only. Apply the rubric exactly. "
        "Do not infer or state injected fault names."
    )
    user_payload = {
        "task": "judge_decision_packet",
        "approved_recovery_actions": dict(approved_actions),
        "packet": sanitized,
    }

    body = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": system_prompt}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": json.dumps(user_payload)}],
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": _JUDGE_SCHEMA_NAME,
                "schema": _judge_schema(),
                "strict": True,
            }
        },
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    errors: list[str] = []
    with httpx.Client(timeout=30.0) as client:
        for _ in range(2):
            try:
                response = client.post(endpoint, headers=headers, json=body)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, Mapping):
                    raise ValueError("response was not a JSON object")
                parsed = _extract_json_text(payload)
                if not isinstance(parsed, Mapping):
                    raise ValueError("judge verdict was not a JSON object")
                return _validate_verdict(parsed, incident_id=incident_id, model=model)
            except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

    return _skip_verdict(
        incident_id,
        model=model,
        reason=f"invalid_model_output_after_retry: {errors[-1]}",
    )


def _build_sensor_result(
    verdicts: list[dict[str, Any]],
    *,
    trusted: bool,
    stage: str,
    now: float,
) -> tuple[dict[str, Any], int]:
    rows = [row for row in verdicts if row.get("status") == "verdict"]
    checked = len(rows)
    skipped = len(verdicts) - checked

    status = "pass"
    severity = "info"
    message = "judge verdict quality within thresholds"
    violations: list[dict[str, Any]] = []
    exit_code = 0

    if checked == 0:
        status = "skipped"
        message = "no verdict packets (all skipped)"
    else:
        diagnosis_true = sum(bool(row["diagnosis_correct"]) for row in rows) / checked
        action_true = sum(bool(row["action_appropriate"]) for row in rows) / checked
        bad_a12 = [
            row
            for row in rows
            if any(check in {"A1", "A2"} for check in row.get("failed_checks", []))
        ]

        if trusted:
            if diagnosis_true < 0.8:
                violations.append(
                    {
                        "incident_id": None,
                        "attempt": None,
                        "detail": f"diagnosis_correct share below threshold: {diagnosis_true:.3f}",
                    }
                )
            if action_true < 0.8:
                violations.append(
                    {
                        "incident_id": None,
                        "attempt": None,
                        "detail": f"action_appropriate share below threshold: {action_true:.3f}",
                    }
                )
            for row in bad_a12:
                violations.append(
                    {
                        "incident_id": row.get("incident_id"),
                        "attempt": None,
                        "detail": f"failed critical checks: {row.get('failed_checks')}",
                    }
                )
            if violations:
                status = "fail"
                severity = "error"
                message = "judge thresholds violated"
                exit_code = 1
            else:
                message = (
                    f"trusted calibration; diagnosis_true={diagnosis_true:.3f}, "
                    f"action_true={action_true:.3f}, skipped={skipped}"
                )
        else:
            status = "pass"
            message = (
                "calibration not trusted; advisory only "
                "(diagnosis_true="
                f"{diagnosis_true:.3f}, action_true={action_true:.3f}, skipped={skipped})"
            )

    result = {
        "schema_version": "v1",
        "sensor_name": "llm_judge_quality",
        "lane": "behavior",
        "sensor_type": "inferential",
        "stage": stage,
        "status": status,
        "severity": severity,
        "message": message,
        "violations": violations,
        "checked": checked,
        "ts": now,
    }
    validate_record(result, "sensor_result")
    return result, exit_code


def _load_calibration(path: Path | None) -> bool:
    if path is None:
        return False
    payload = json.loads(path.read_text(encoding="utf-8"))
    return bool(payload.get("trusted"))


def _load_langsmith_client_class() -> type[Any]:
    try:
        from langsmith import Client
    except ImportError as exc:
        raise RuntimeError("langsmith is not installed; rerun with the trace extra") from exc
    return Client


def _mirror_to_langsmith(
    *,
    packets: list[dict[str, Any]],
    verdicts: list[dict[str, Any]],
) -> None:
    Client = _load_langsmith_client_class()
    client = Client()

    dataset_name = "mi-review-packets"
    dataset_id: str | None = None
    try:
        dataset = client.create_dataset(
            dataset_name=dataset_name,
            description="Weekly harness review packets",
        )
        value = dataset.get("id") if isinstance(dataset, Mapping) else getattr(dataset, "id", None)
        dataset_id = str(value) if value is not None else None
    except Exception:  # dataset may already exist
        for dataset in client.list_datasets(dataset_name=dataset_name):
            value = (
                dataset.get("id")
                if isinstance(dataset, Mapping)
                else getattr(dataset, "id", None)
            )
            if value is not None:
                dataset_id = str(value)
                break

    if dataset_id is not None:
        for packet in packets:
            client.create_example(inputs=packet, outputs={}, dataset_id=dataset_id)

    project = os.getenv("LANGSMITH_PROJECT", "mi-review-judge")
    for verdict in verdicts:
        client.create_run(
            name="harness.review.judge",
            run_type="chain",
            project_name=project,
            inputs={"incident_id": verdict.get("incident_id")},
            outputs=verdict,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m harness.review.judge",
        description="Judge sampled decision packets and emit verdict JSONL + sensor_result.",
    )
    parser.add_argument("--events", type=Path, default=Path("state/events.jsonl"))
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--model", default="gpt-5.3")
    parser.add_argument("--endpoint", default=_OPENAI_ENDPOINT)
    parser.add_argument("--langsmith", action="store_true")
    parser.add_argument("--calibration", type=Path)
    parser.add_argument("--stage", choices=("continuous",), default="continuous")
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

    api_key = os.getenv("OPENAI_API_KEY")
    if api_key is None or api_key == "":
        print("OPENAI_API_KEY is required", file=sys.stderr)
        return 2

    if args.langsmith:
        try:
            _load_langsmith_client_class()
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    try:
        trusted = _load_calibration(args.calibration)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"invalid calibration file: {exc}", file=sys.stderr)
        return 2

    try:
        events = load_event_log(args.events)
        packets = sample_packets(events, n=args.n)
        rubric_text = _read_rubric()
        approved_actions = _extract_approved_actions_sections()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    verdicts: list[dict[str, Any]] = []
    for packet in packets:
        verdict = _judge_packet(
            packet,
            model=args.model,
            endpoint=args.endpoint,
            api_key=api_key,
            rubric_text=rubric_text,
            approved_actions=approved_actions,
        )
        verdicts.append(verdict)
        print(json.dumps(verdict, sort_keys=True))

    sensor, exit_code = _build_sensor_result(
        verdicts,
        trusted=trusted,
        stage=args.stage,
        now=time.time(),
    )
    print(json.dumps(sensor, sort_keys=True))

    if args.langsmith:
        _mirror_to_langsmith(packets=packets, verdicts=verdicts)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
