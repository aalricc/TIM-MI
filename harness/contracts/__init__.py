from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

CONTRACT_DIR = Path(__file__).resolve().parent

CONTRACT_NAMES = (
    "incident_signature",
    "problem_class",
    "class_action_stats",
    "skill_entry",
    "scenario_fixture",
    "review_verdict",
    "sensor_result",
    "decision_trace",
    "workflow_step",
)


@cache
def load_schema(name: str, version: str = "v1") -> dict[str, Any]:
    if name not in CONTRACT_NAMES:
        raise KeyError(f"unknown harness contract: {name}")
    path = CONTRACT_DIR / f"{name}.schema.{version}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"contract is not a JSON object: {path}")
    return payload


def contract_errors(record: Any, name: str, version: str = "v1") -> list[str]:
    validator = Draft202012Validator(load_schema(name, version))
    errors = sorted(validator.iter_errors(record), key=lambda err: list(err.absolute_path))
    return [
        f"{'/'.join(str(part) for part in err.absolute_path) or '<root>'}: {err.message}"
        for err in errors
    ]


def validate_record(record: Any, name: str, version: str = "v1") -> None:
    errors = contract_errors(record, name, version)
    if errors:
        raise ValueError(f"{name}.{version} contract violation: " + "; ".join(errors))
