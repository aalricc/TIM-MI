from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from harness.contracts import contract_errors
from harness.mappings.runtime import (
    class_action_stats,
    decision_traces,
    incident_signatures,
    problem_classes,
    skill_entries,
    workflow_step_record,
    workflow_steps,
)
from tests.harness_scenarios import DIAGNOSIS, SCENARIOS, ScenarioRun


def _assert_valid(records: list[dict[str, Any]], contract: str) -> None:
    for record in records:
        errors = contract_errors(record, contract)
        assert errors == [], f"{contract}: {errors}\n{record}"


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
async def test_runtime_outputs_map_to_valid_contract_records(
    tmp_path: Path, scenario: str
) -> None:
    run = await SCENARIOS[scenario](tmp_path)
    events = run.events

    steps = workflow_steps(events)
    traces = decision_traces(events)
    signatures = incident_signatures(events)
    assert steps and traces and signatures

    _assert_valid(steps, "workflow_step")
    _assert_valid(traces, "decision_trace")
    _assert_valid(signatures, "incident_signature")
    _assert_valid(problem_classes(run.class_store), "problem_class")
    _assert_valid(class_action_stats(run.class_store), "class_action_stats")
    _assert_valid(skill_entries(run.skills_text), "skill_entry")


@pytest.mark.asyncio
async def test_workflow_steps_follow_readme_step_order(tmp_path: Path) -> None:
    run = await SCENARIOS["full_loop_success"](tmp_path)

    readme_steps = [record["readme_step"] for record in workflow_steps(run.events)]

    assert readme_steps == [
        1, 2, 3, 4, 5, 5, 6, 7, 8, 8, 9, 10, 11, 12, 13, 14, 15, 16, None, None
    ]


@pytest.mark.asyncio
async def test_decision_traces_cover_usable_and_unusable_outcomes(tmp_path: Path) -> None:
    run = await SCENARIOS["unusable_decisions_then_success"](tmp_path)

    traces = decision_traces(run.events)

    assert [(t["attempt"], t["outcome"], t["reason"]) for t in traces] == [
        (1, "unusable", "low_confidence"),
        (2, "unusable", "jev_request_error:TypeSafeAPITimeoutError"),
        (3, "unusable", "selected_candidate_not_found"),
        (4, "selected", "usable"),
    ]
    not_found = traces[2]
    assert not_found["decision"]["usable"] is True
    assert not_found["selected_candidate"] is None
    assert not_found["candidates_origin"] == "jev_evaluated"
    assert {row["operation"] for row in not_found["candidates"]} >= {"restart_container"}

    selected = traces[3]
    assert selected["source"] == "full_loop"
    assert selected["candidates_origin"] == "how_proposal"
    assert selected["evaluated_count"] == 3
    assert selected["selected_candidate"] == {
        "name": "recreate_widgets",
        "operation": "run_sql",
        "executed_candidate": "primary",
        "executed_tool_name": "recreate_widgets",
    }


@pytest.mark.asyncio
async def test_decision_traces_record_fast_path_fallback_then_full_loop(tmp_path: Path) -> None:
    run = await SCENARIOS["fast_path_rejected_falls_back"](tmp_path)

    traces = decision_traces(run.events)

    assert [(t["attempt"], t["source"], t["outcome"]) for t in traces] == [
        (1, "class_memory", "fast_path_fallback"),
        (1, "full_loop", "selected"),
    ]
    fallback = traces[0]
    assert fallback["reason"] == "reject:class_memory"
    assert fallback["candidates_origin"] == "not_emitted"
    assert fallback["evaluated_count"] == 1
    assert fallback["class_id"] is not None
    assert traces[1]["class_id"] == fallback["class_id"]


@pytest.mark.asyncio
async def test_fast_path_selection_trace_carries_class_context(tmp_path: Path) -> None:
    run = await SCENARIOS["fast_path_failure_then_full_loop"](tmp_path)

    traces = decision_traces(run.events)

    assert [(t["attempt"], t["source"], t["outcome"]) for t in traces] == [
        (1, "class_memory", "selected"),
        (2, "full_loop", "selected"),
    ]
    assert traces[0]["evaluated_count"] == 1
    assert traces[0]["action_key"] != traces[1]["action_key"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
async def test_incident_signature_is_one_record_per_incident(
    tmp_path: Path, scenario: str
) -> None:
    run = await SCENARIOS[scenario](tmp_path)

    signatures = incident_signatures(run.events)
    started = [e for e in run.events if e["event_type"] == "incident.started"]

    assert [s["incident_id"] for s in signatures] == [e["payload"]["incident_id"] for e in started]
    for signature in signatures:
        assert signature["started_at"] is not None
        assert signature["monitor_detail"] == "health_endpoint_unhealthy"
        assert signature["failure_type"] == DIAGNOSIS.failure_type
        assert signature["hypotheses"] == DIAGNOSIS.hypotheses
        has_class_memory = run.class_store["classes"] != {}
        assert (signature["class_match"] is not None) == has_class_memory


def _assert_store_records_consistent(run: ScenarioRun) -> None:
    classes = problem_classes(run.class_store)
    stats = class_action_stats(run.class_store)
    assert len(classes) == 1
    assert classes[0]["action_keys"] == sorted(s["action_key"] for s in stats)
    for row in stats:
        assert row["attempts"] == row["successes"] + row["failures"]


@pytest.mark.asyncio
async def test_class_store_records_after_fast_path_failure(tmp_path: Path) -> None:
    run = await SCENARIOS["fast_path_failure_then_full_loop"](tmp_path)

    _assert_store_records_consistent(run)
    stats = {row["action_name"]: row for row in class_action_stats(run.class_store)}
    # Seeded with 8 successes, then failed once on the fast path.
    assert stats["class_memory_recreate"]["successes"] == 8
    assert stats["class_memory_recreate"]["failures"] == 1
    assert stats["class_memory_recreate"]["source"] == "class_memory"
    winner = stats["recreate_widgets"]
    assert (winner["successes"], winner["failures"], winner["source"]) == (1, 0, "full_loop")
    assert problem_classes(run.class_store)[0]["skill_anchor"] == winner["action_key"]


@pytest.mark.asyncio
async def test_class_store_records_after_cold_start(tmp_path: Path) -> None:
    run = await SCENARIOS["class_memory_cold_start"](tmp_path)

    _assert_store_records_consistent(run)
    signature = incident_signatures(run.events)[0]
    assert signature["class_match"]["new_class"] is True
    assert signature["class_match"]["first_failing_gate_reason"] == "no_actions"


@pytest.mark.asyncio
async def test_skill_entries_parse_legacy_blocks(tmp_path: Path) -> None:
    run = await SCENARIOS["failure_then_unhealthy_ping_then_success"](tmp_path)

    entries = skill_entries(run.skills_text)

    # Persistence ran twice: attempt 2 (then unhealthy ping) and attempt 3.
    assert [e["format"] for e in entries] == ["legacy_block", "legacy_block"]
    first, second = entries
    assert first["skill_id"] == "recovery-recreate_widgets"
    assert first["tool_name"] == "recreate_widgets"
    assert first["operation"] == "run_sql"
    assert first["failure_type"] == DIAGNOSIS.failure_type
    assert (first["attempts_to_success"], second["attempts_to_success"]) == (2, 3)
    # Attempt 1 failed with the same tool (-1 reward), so RL attempts are 2 then 3
    # and the version patch is rl_attempts - 1.
    assert (first["rl_attempts"], second["rl_attempts"]) == (2.0, 3.0)
    assert (first["rl_reward_total"], second["rl_reward_total"]) == (0.0, 1.0)
    assert (first["version"], second["version"]) == ("1.0.1", "1.0.2")


@pytest.mark.asyncio
async def test_skill_entries_parse_class_sections(tmp_path: Path) -> None:
    run = await SCENARIOS["fast_path_failure_then_full_loop"](tmp_path)

    entries = skill_entries(run.skills_text)
    class_id = problem_classes(run.class_store)[0]["class_id"]

    assert len(entries) == 1
    entry = entries[0]
    assert entry["format"] == "class_section"
    assert entry["skill_id"] == f"class:{class_id}"
    assert entry["champion_template"]["name"] == "class_memory_recreate"
    stats = {row["action_key"]: row for row in class_action_stats(run.class_store)}
    assert entry["champion_action_key"] in stats


@pytest.mark.asyncio
async def test_cold_start_success_writes_skill_section(tmp_path: Path) -> None:
    # Step 15 renders the pending success, so a brand-new class gets its skill on
    # its first recovery and the section agrees with what step 16 persisted.
    run = await SCENARIOS["class_memory_cold_start"](tmp_path)

    persist_skills = [s for s in workflow_steps(run.events) if s["step"] == "persist.skills"]
    entries = skill_entries(run.skills_text)
    stats = class_action_stats(run.class_store)

    assert [s["status"] for s in persist_skills] == ["ok"]
    assert len(entries) == 1 and len(stats) == 1
    assert entries[0]["champion_action_key"] == stats[0]["action_key"]
    assert entries[0]["attempts"] == stats[0]["attempts"] == 1
    assert entries[0]["last_verified_ts"] == stats[0]["last_ok_ts"]


def test_skill_entries_parse_mixed_file() -> None:
    text = (
        "# Recovery Skills Library\n\nAuto-learned skills from successful recovery sessions.\n"
        "\n---\nname: recovery-fix\ndescription: d\ntriggers:\n  - \"ft\"\n"
        "  - \"recover ft\"\ntools:\n  - run_sql\nversion: 1.0.0\n---\n\n# Skill: fix\n\n"
        "## Learned From Successful Session\n- timestamp: 2026-10-20T05:43:57Z\n"
        "- incident_id: inc-1\n- attempts_to_success: 1\n- failure_type: ft\n\n"
        "## RL Knowledge\n- action_key: fix\n- rl_attempts: 1\n- rl_reward_total: 1.00\n"
        "- rl_avg_reward: 1.00\n"
        "\n<!-- class:abc begin -->\n# Class abc\n\n- champion_action_key: k\n"
        "- champion_lcb: 0.5000\n- attempts: 2\n- mean_latency_ms: 1.00\n"
        "- last_verified_ts: None\n- runner_ups: none\n- templates: none\n\n"
        "## Champion Template\n{\n  \"name\": \"n\"\n}\n<!-- class:abc end -->\n"
    )

    entries = skill_entries(text)

    assert [e["format"] for e in entries] == ["class_section", "legacy_block"]
    assert entries[0]["last_verified_ts"] is None
    assert entries[0]["runner_ups"] == [] and entries[0]["templates"] == []
    assert entries[1]["rl_avg_reward"] == 1.0
    _assert_valid(entries, "skill_entry")


def test_decision_trace_skips_event_log_truncation_markers() -> None:
    events = [
        {
            "ts": 1.0,
            "event_type": "workflow.step",
            "payload": {
                "incident_id": "inc-1",
                "attempt": 2,
                "path_mode": "full_loop",
                "step": "path.selected",
                "status": "ok",
            },
        },
        {
            "ts": 2.0,
            "event_type": "decision.unusable",
            "payload": {
                "incident_id": "inc-1",
                "reason": "low_confidence",
                "candidate_actions": [
                    {"name": "a", "operation": "run_sql", "params": {"sql": "x"}},
                    "<truncated:3 items>",
                ],
                "decision": {"usable": False, "reason": "low_confidence"},
                "selected_action_key": None,
                "attempts": 2,
            },
        },
    ]

    traces = decision_traces(events)

    assert len(traces) == 1
    assert traces[0]["attempt"] == 2
    assert traces[0]["source"] == "full_loop"
    assert traces[0]["evaluated_count"] == 1
    _assert_valid(traces, "decision_trace")


@pytest.mark.asyncio
async def test_mappings_do_not_mutate_runtime_inputs(tmp_path: Path) -> None:
    run = await SCENARIOS["fast_path_failure_then_full_loop"](tmp_path)
    events = run.events
    store = run.class_store
    events_before = copy.deepcopy(events)
    store_before = copy.deepcopy(store)

    workflow_steps(events)
    decision_traces(events)
    incident_signatures(events)
    problem_classes(store)
    class_action_stats(store)

    assert events == events_before
    assert store == store_before


def test_unknown_runtime_step_is_rejected_by_contract() -> None:
    record = workflow_step_record(
        {
            "ts": 1.0,
            "event_type": "workflow.step",
            "payload": {
                "incident_id": "inc-1",
                "attempt": 1,
                "path_mode": "full_loop",
                "step": "llm.reflect",
                "status": "ok",
            },
        }
    )

    assert record["readme_step"] is None
    assert any(error.startswith("step:") for error in contract_errors(record, "workflow_step"))


def test_workflow_step_record_rejects_other_event_types() -> None:
    with pytest.raises(ValueError):
        workflow_step_record({"ts": 1.0, "event_type": "monitor.health", "payload": {}})
