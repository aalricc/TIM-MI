"""Architecture-fitness sensors over runtime `workflow.step` timelines.

Each sensor is a pure function of the event log and returns a `sensor_result` v1
record. Sensors observe only; they never feed back into runtime control flow.

Usage:
    python -m harness.sensors.runtime_invariants state/events.jsonl
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from harness.mappings.runtime import RuntimeEvent, load_event_log, workflow_steps

PERSIST_ORDER = (
    "persist.tool_catalog",
    "persist.policy_store",
    "persist.skills",
    "persist.class_memory",
)
# Failure-path learning writes (negative reward, failed class outcome), README order.
FAILURE_PERSIST_ORDER = ("persist.policy_store", "persist.class_memory")

AttemptKey = tuple[str, int]
Violation = dict[str, Any]


def _attempt_timelines(events: Iterable[RuntimeEvent]) -> dict[AttemptKey, list[dict[str, Any]]]:
    timelines: dict[AttemptKey, list[dict[str, Any]]] = {}
    for step in workflow_steps(events):
        incident_id = step.get("incident_id")
        attempt = step.get("attempt")
        if not isinstance(incident_id, str) or not isinstance(attempt, int):
            continue
        timelines.setdefault((incident_id, attempt), []).append(step)
    return timelines


def _violation(key: AttemptKey, detail: str) -> Violation:
    return {"incident_id": key[0], "attempt": key[1], "detail": detail}


def _result(
    *,
    name: str,
    stage: str,
    violations: list[Violation],
    checked: int,
    ok_message: str,
    now: float | None,
) -> dict[str, Any]:
    failed = bool(violations)
    return {
        "schema_version": "v1",
        "sensor_name": name,
        "lane": "architecture",
        "sensor_type": "computational",
        "stage": stage,
        "status": "fail" if failed else "pass",
        "severity": "error" if failed else "info",
        "message": f"{len(violations)} violation(s)" if failed else ok_message,
        "violations": violations,
        "checked": checked,
        "ts": time.time() if now is None else now,
    }


def jev_selects_before_registration(
    events: Iterable[RuntimeEvent], *, stage: str = "ci", now: float | None = None
) -> dict[str, Any]:
    """Every dynamic_tool.registered is preceded, in the same attempt and path mode,
    by a usable jev.result (README step 8 is mandatory)."""
    violations: list[Violation] = []
    checked = 0
    for key, steps in _attempt_timelines(events).items():
        for index, step in enumerate(steps):
            if step["step"] != "dynamic_tool.registered":
                continue
            checked += 1
            usable_before = any(
                prior["step"] == "jev.result"
                and prior["status"] == "usable"
                and prior["path_mode"] == step["path_mode"]
                for prior in steps[:index]
            )
            if not usable_before:
                violations.append(
                    _violation(key, f"{step['path_mode']} registration without usable jev.result")
                )
    return _result(
        name="jev_selects_before_registration",
        stage=stage,
        violations=violations,
        checked=checked,
        ok_message="every registered action was selected by a usable JEV decision",
        now=now,
    )


def security_gate_before_registration(
    events: Iterable[RuntimeEvent], *, stage: str = "ci", now: float | None = None
) -> dict[str, Any]:
    """Every dynamic_tool.registered is immediately preceded, in the same attempt and
    path mode, by security.gate=ok (README step 9 before step 10). A rejected gate ends
    the attempt as attempt.result=failed with nothing registered or executed after it."""
    violations: list[Violation] = []
    checked = 0
    for key, steps in _attempt_timelines(events).items():
        for index, step in enumerate(steps):
            if step["step"] == "security.gate" and step["status"] == "rejected":
                checked += 1
                after = [(s["step"], s["status"]) for s in steps[index + 1 :]]
                names = [name for name, _ in after]
                if "dynamic_tool.registered" in names or "dynamic_tool.executed" in names:
                    violations.append(_violation(key, "action ran after security.gate=rejected"))
                if ("attempt.result", "failed") not in after:
                    violations.append(
                        _violation(key, "security.gate=rejected without attempt.result=failed")
                    )
                continue
            if step["step"] != "dynamic_tool.registered":
                continue
            checked += 1
            prior = steps[index - 1] if index > 0 else None
            if (
                prior is None
                or prior["step"] != "security.gate"
                or prior["status"] != "ok"
                or prior["path_mode"] != step["path_mode"]
            ):
                violations.append(
                    _violation(key, "registration not immediately preceded by security.gate=ok")
                )
    return _result(
        name="security_gate_before_registration",
        stage=stage,
        violations=violations,
        checked=checked,
        ok_message="every registered action passed the security gate first",
        now=now,
    )


def one_action_per_attempt(
    events: Iterable[RuntimeEvent], *, stage: str = "ci", now: float | None = None
) -> dict[str, Any]:
    """At most one dynamic_tool.executed per attempt, and only after registration."""
    violations: list[Violation] = []
    timelines = _attempt_timelines(events)
    for key, steps in timelines.items():
        names = [step["step"] for step in steps]
        executed = names.count("dynamic_tool.executed")
        if executed > 1:
            violations.append(_violation(key, f"{executed} dynamic actions executed"))
        if executed and "dynamic_tool.registered" not in names[
            : names.index("dynamic_tool.executed")
        ]:
            violations.append(_violation(key, "dynamic_tool.executed without prior registration"))
    return _result(
        name="one_action_per_attempt",
        stage=stage,
        violations=violations,
        checked=len(timelines),
        ok_message="no attempt executed more than one dynamic action",
        now=now,
    )


def verification_follows_execution(
    events: Iterable[RuntimeEvent], *, stage: str = "ci", now: float | None = None
) -> dict[str, Any]:
    """Every executed action is followed by the independent verifier (README step 12)
    before the attempt ends."""
    violations: list[Violation] = []
    checked = 0
    for key, steps in _attempt_timelines(events).items():
        names = [step["step"] for step in steps]
        if "dynamic_tool.executed" not in names:
            continue
        checked += 1
        after = names[names.index("dynamic_tool.executed") + 1 :]
        if "verification.result" not in after:
            violations.append(_violation(key, "executed action has no verification.result"))
    return _result(
        name="verification_follows_execution",
        stage=stage,
        violations=violations,
        checked=checked,
        ok_message="every executed action was independently verified",
        now=now,
    )


def persistence_only_after_verified_recovery(
    events: Iterable[RuntimeEvent], *, stage: str = "ci", now: float | None = None
) -> dict[str, Any]:
    """Success persistence (persist.* with status other than `failure`, README 13-16)
    appears only after verification.result=ok in the same attempt, each step once, in
    order 13 -> 14 -> 15 -> 16."""
    violations: list[Violation] = []
    checked = 0
    for key, steps in _attempt_timelines(events).items():
        names = [
            step["step"] if step["status"] != "failure" else "<failure-write>" for step in steps
        ]
        persisted = [name for name in names if name in PERSIST_ORDER]
        if not persisted:
            continue
        checked += 1
        first_persist = names.index(persisted[0])
        verified_ok = any(
            step["step"] == "verification.result" and step["status"] == "ok"
            for step in steps[:first_persist]
        )
        if not verified_ok:
            violations.append(_violation(key, "persistence without prior verification.result=ok"))
        if persisted != list(PERSIST_ORDER):
            violations.append(
                _violation(key, f"persist steps {persisted} != expected {list(PERSIST_ORDER)}")
            )
    return _result(
        name="persistence_only_after_verified_recovery",
        stage=stage,
        violations=violations,
        checked=checked,
        ok_message="success persistence ran only after verified recovery, in step order",
        now=now,
    )


def failure_learning_only_on_failed_attempts(
    events: Iterable[RuntimeEvent], *, stage: str = "ci", now: float | None = None
) -> dict[str, Any]:
    """Failure-path learning writes (persist.* status=failure) occur only in attempts
    that end with attempt.result=failed, never after verification.result=ok, at most
    once per store, policy (14) before class memory (16)."""
    violations: list[Violation] = []
    checked = 0
    for key, steps in _attempt_timelines(events).items():
        writes = [step["step"] for step in steps if step["status"] == "failure"]
        if not writes:
            continue
        checked += 1
        outcomes = [step["status"] for step in steps if step["step"] == "attempt.result"]
        if outcomes != ["failed"]:
            violations.append(_violation(key, f"failure writes in attempt ending {outcomes}"))
        if any(
            step["step"] == "verification.result" and step["status"] == "ok" for step in steps
        ):
            violations.append(_violation(key, "failure writes after verification.result=ok"))
        expected = [name for name in FAILURE_PERSIST_ORDER if name in writes]
        if writes != expected:
            violations.append(
                _violation(key, f"failure writes {writes} != expected order {expected}")
            )
    return _result(
        name="failure_learning_only_on_failed_attempts",
        stage=stage,
        violations=violations,
        checked=checked,
        ok_message="failure-path learning writes only on failed attempts, in step order",
        now=now,
    )


def retry_resumes_at_step5(
    events: Iterable[RuntimeEvent], *, stage: str = "ci", now: float | None = None
) -> dict[str, Any]:
    """After attempt.result retry/failed, the incident's next attempt starts with
    context.reused followed by class_memory.lookup (loop back to README step 5)."""
    violations: list[Violation] = []
    checked = 0
    timelines = _attempt_timelines(events)
    retry_from: set[AttemptKey] = {
        key
        for key, steps in timelines.items()
        if any(
            step["step"] == "attempt.result" and step["status"] in {"retry", "failed"}
            for step in steps
        )
    }
    for incident_id, attempt in sorted(retry_from):
        next_key = (incident_id, attempt + 1)
        steps = timelines.get(next_key)
        if steps is None:
            continue  # incident ended or log window closed before the next attempt
        checked += 1
        names = [step["step"] for step in steps]
        if names[:1] != ["context.reused"]:
            violations.append(
                _violation(next_key, f"retry attempt starts with {names[:1]}, not context.reused")
            )
        elif names[1:2] != ["class_memory.lookup"]:
            violations.append(
                _violation(next_key, f"context.reused followed by {names[1:2]}, not step 5 lookup")
            )
    return _result(
        name="retry_resumes_at_step5",
        stage=stage,
        violations=violations,
        checked=checked,
        ok_message="every retry resumed at step 5 with retained context",
        now=now,
    )


SENSORS: tuple[Callable[..., dict[str, Any]], ...] = (
    jev_selects_before_registration,
    security_gate_before_registration,
    one_action_per_attempt,
    verification_follows_execution,
    persistence_only_after_verified_recovery,
    failure_learning_only_on_failed_attempts,
    retry_resumes_at_step5,
)


def run_all(
    events: Iterable[RuntimeEvent], *, stage: str = "ci", now: float | None = None
) -> list[dict[str, Any]]:
    rows = list(events)
    return [sensor(rows, stage=stage, now=now) for sensor in SENSORS]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m harness.sensors.runtime_invariants <events.jsonl>", file=sys.stderr)
        return 2
    results = run_all(load_event_log(Path(argv[1])), stage="continuous")
    print(json.dumps(results, indent=2))
    return 1 if any(result["status"] == "fail" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
