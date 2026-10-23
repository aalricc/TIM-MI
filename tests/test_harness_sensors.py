from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from harness.contracts import contract_errors
from harness.sensors.runtime_invariants import (
    failure_learning_only_on_failed_attempts,
    jev_selects_before_registration,
    main,
    one_action_per_attempt,
    persistence_only_after_verified_recovery,
    retry_resumes_at_step5,
    run_all,
    security_gate_before_registration,
    verification_follows_execution,
)
from tests.harness_scenarios import SCENARIOS

Event = dict[str, Any]
Sensor = Callable[..., dict[str, Any]]


def _is_step(event: Event, step: str, **fields: Any) -> bool:
    payload = event.get("payload", {})
    return (
        event.get("event_type") == "workflow.step"
        and payload.get("step") == step
        and all(payload.get(key) == value for key, value in fields.items())
    )


def _index(events: list[Event], step: str, **fields: Any) -> int:
    return next(i for i, e in enumerate(events) if _is_step(e, step, **fields))


def _assert_fails(sensor: Sensor, events: list[Event]) -> dict[str, Any]:
    result = sensor(events, now=1.0)
    assert result["status"] == "fail", result
    assert result["violations"]
    assert contract_errors(result, "sensor_result") == []
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
async def test_all_sensors_pass_on_runtime_scenarios(tmp_path: Path, scenario: str) -> None:
    run = await SCENARIOS[scenario](tmp_path)

    results = run_all(run.events, now=1.0)

    for result in results:
        assert contract_errors(result, "sensor_result") == []
        assert result["status"] == "pass", result
        assert result["lane"] == "architecture"


@pytest.mark.asyncio
async def test_sensors_examine_the_expected_units(tmp_path: Path) -> None:
    run = await SCENARIOS["failure_then_unhealthy_ping_then_success"](tmp_path)

    checked = {r["sensor_name"]: r["checked"] for r in run_all(run.events, now=1.0)}

    # Attempt 1 fails verification (failure writes), attempts 2-3 persist success.
    assert checked == {
        "jev_selects_before_registration": 3,
        "security_gate_before_registration": 3,
        "one_action_per_attempt": 3,
        "verification_follows_execution": 3,
        "persistence_only_after_verified_recovery": 2,
        "failure_learning_only_on_failed_attempts": 1,
        "retry_resumes_at_step5": 2,
    }


@pytest.mark.asyncio
async def test_jev_sensor_flags_registration_without_usable_decision(tmp_path: Path) -> None:
    events = (await SCENARIOS["full_loop_success"](tmp_path)).events
    events[_index(events, "jev.result")]["payload"]["status"] = "unusable"

    _assert_fails(jev_selects_before_registration, events)


@pytest.mark.asyncio
async def test_jev_sensor_requires_matching_path_mode(tmp_path: Path) -> None:
    # A usable fast-path decision must not license a full-loop registration.
    events = (await SCENARIOS["fast_path_rejected_falls_back"](tmp_path)).events
    events[_index(events, "jev.result", path_mode="fast_path")]["payload"]["status"] = "usable"
    events[_index(events, "jev.result", path_mode="full_loop")]["payload"]["status"] = "unusable"

    _assert_fails(jev_selects_before_registration, events)


@pytest.mark.asyncio
async def test_gate_sensor_flags_registration_without_gate(tmp_path: Path) -> None:
    events = (await SCENARIOS["full_loop_success"](tmp_path)).events
    del events[_index(events, "security.gate")]

    _assert_fails(security_gate_before_registration, events)


@pytest.mark.asyncio
async def test_gate_sensor_flags_rejected_gate(tmp_path: Path) -> None:
    events = (await SCENARIOS["full_loop_success"](tmp_path)).events
    events[_index(events, "security.gate")]["payload"]["status"] = "rejected"

    _assert_fails(security_gate_before_registration, events)


@pytest.mark.asyncio
async def test_gate_sensor_flags_action_after_rejection(tmp_path: Path) -> None:
    events = (await SCENARIOS["gate_rejected_then_success"](tmp_path)).events
    rejected = _index(events, "security.gate", status="rejected")
    registered = copy.deepcopy(events[_index(events, "dynamic_tool.registered")])
    registered["payload"].update({"attempt": 1, "path_mode": "fast_path"})
    events.insert(rejected + 1, registered)

    result = _assert_fails(security_gate_before_registration, events)
    details = {v["detail"] for v in result["violations"]}
    assert "action ran after security.gate=rejected" in details


@pytest.mark.asyncio
async def test_gate_sensor_flags_rejection_without_failed_attempt(tmp_path: Path) -> None:
    events = (await SCENARIOS["gate_rejected_then_success"](tmp_path)).events
    del events[_index(events, "attempt.result", attempt=1)]

    result = _assert_fails(security_gate_before_registration, events)
    assert result["violations"][0]["detail"] == (
        "security.gate=rejected without attempt.result=failed"
    )


@pytest.mark.asyncio
async def test_failure_learning_sensor_flags_writes_on_recovered_attempt(
    tmp_path: Path,
) -> None:
    events = (await SCENARIOS["failure_then_unhealthy_ping_then_success"](tmp_path)).events
    write = copy.deepcopy(events[_index(events, "persist.policy_store", status="failure")])
    write["payload"]["attempt"] = 3
    events.insert(_index(events, "attempt.result", attempt=3), write)

    result = _assert_fails(failure_learning_only_on_failed_attempts, events)
    details = {v["detail"] for v in result["violations"]}
    assert "failure writes after verification.result=ok" in details


@pytest.mark.asyncio
async def test_failure_learning_sensor_flags_out_of_order_writes(tmp_path: Path) -> None:
    events = (await SCENARIOS["fast_path_failure_then_full_loop"](tmp_path)).events
    policy = _index(events, "persist.policy_store", status="failure")
    class_memory = _index(events, "persist.class_memory", status="failure")
    events[policy], events[class_memory] = events[class_memory], events[policy]

    _assert_fails(failure_learning_only_on_failed_attempts, events)


@pytest.mark.asyncio
async def test_one_action_sensor_flags_second_execution(tmp_path: Path) -> None:
    events = (await SCENARIOS["full_loop_success"](tmp_path)).events
    index = _index(events, "dynamic_tool.executed")
    events.insert(index + 1, copy.deepcopy(events[index]))

    result = _assert_fails(one_action_per_attempt, events)
    assert result["violations"][0]["detail"] == "2 dynamic actions executed"


@pytest.mark.asyncio
async def test_one_action_sensor_flags_execution_without_registration(tmp_path: Path) -> None:
    events = (await SCENARIOS["full_loop_success"](tmp_path)).events
    del events[_index(events, "dynamic_tool.registered")]

    _assert_fails(one_action_per_attempt, events)


@pytest.mark.asyncio
async def test_verification_sensor_flags_missing_verifier(tmp_path: Path) -> None:
    events = (await SCENARIOS["full_loop_success"](tmp_path)).events
    del events[_index(events, "verification.result")]

    _assert_fails(verification_follows_execution, events)


@pytest.mark.asyncio
async def test_persistence_sensor_flags_persist_after_failed_verification(tmp_path: Path) -> None:
    events = (await SCENARIOS["full_loop_success"](tmp_path)).events
    events[_index(events, "verification.result")]["payload"]["status"] = "failed"

    _assert_fails(persistence_only_after_verified_recovery, events)


@pytest.mark.asyncio
async def test_persistence_sensor_flags_out_of_order_steps(tmp_path: Path) -> None:
    events = (await SCENARIOS["full_loop_success"](tmp_path)).events
    skills = _index(events, "persist.skills")
    class_memory = _index(events, "persist.class_memory")
    events[skills], events[class_memory] = events[class_memory], events[skills]

    _assert_fails(persistence_only_after_verified_recovery, events)


@pytest.mark.asyncio
async def test_retry_sensor_flags_retry_that_restarts_at_step2(tmp_path: Path) -> None:
    events = (await SCENARIOS["fast_path_failure_then_full_loop"](tmp_path)).events
    events[_index(events, "context.reused")]["payload"]["step"] = "diagnostics.collected"

    result = _assert_fails(retry_resumes_at_step5, events)
    assert result["violations"][0]["attempt"] == 2


@pytest.mark.asyncio
async def test_retry_sensor_flags_retry_that_skips_class_memory_lookup(tmp_path: Path) -> None:
    events = (await SCENARIOS["fast_path_failure_then_full_loop"](tmp_path)).events
    del events[_index(events, "class_memory.lookup", attempt=2)]

    _assert_fails(retry_resumes_at_step5, events)


def _write_log(path: Path, events: list[Event]) -> Path:
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    events = (await SCENARIOS["full_loop_success"](tmp_path)).events
    clean = _write_log(tmp_path / "clean.jsonl", events)
    del events[_index(events, "verification.result")]
    broken = _write_log(tmp_path / "broken.jsonl", events)

    assert main(["prog", str(clean)]) == 0
    clean_results = json.loads(capsys.readouterr().out)
    assert {r["stage"] for r in clean_results} == {"continuous"}
    assert main(["prog", str(broken)]) == 1
    capsys.readouterr()
    assert main(["prog"]) == 2
