from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from harness.contracts import contract_errors
from harness.review.sample import main as sample_main
from harness.review.sample import packet_markdown, review_verdict_template, sample_packets
from tests.harness_scenarios import SCENARIOS


def _write_jsonl(path: Path, events: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_sample_packets_include_signature_trace_and_outcome(tmp_path: Path) -> None:
    run = await SCENARIOS["full_loop_success"](tmp_path)

    packets = sample_packets(run.events, n=20)

    assert len(packets) == 1
    packet = packets[0]
    assert isinstance(packet["incident_id"], str) and packet["incident_id"] != ""
    assert packet["incident_signature"]["incident_id"] == packet["incident_id"]
    assert packet["outcome"]["status"] == "recovered"
    assert packet["outcome"]["selected_operations"]
    assert contract_errors(packet["incident_signature"], "incident_signature") == []
    assert contract_errors(packet["decision_traces"][0], "decision_trace") == []

    rendered = packet_markdown(packet)
    assert rendered.startswith("# Review Packet:")
    assert "## Incident Signature" in rendered
    assert "## Decision Traces" in rendered
    assert "## Reviewer Verdict (`review_verdict.schema.v1.json`)" in rendered


def test_review_verdict_template_is_invalid_until_filled_in() -> None:
    template = review_verdict_template("inc-1")

    assert contract_errors(template, "review_verdict") != []

    filled = {**template, "diagnosis_correct": True, "action_appropriate": False}
    assert contract_errors(filled, "review_verdict") == []


@pytest.mark.asyncio
async def test_outcome_carries_recovery_data_loss_fields(tmp_path: Path) -> None:
    run = await SCENARIOS["full_loop_success"](tmp_path)
    events = run.events
    recovered = next(e for e in events if e["event_type"] == "incident.recovered")
    recovered["payload"] = {**recovered["payload"], "rows_lost": 3, "reward": 0.9}

    outcome = sample_packets(events, n=1)[0]["outcome"]

    assert (outcome["rows_lost"], outcome["reward"]) == (3, 0.9)


@pytest.mark.asyncio
async def test_packets_are_newest_first_and_cut_at_n(tmp_path: Path) -> None:
    older = await SCENARIOS["full_loop_success"](tmp_path / "older")
    newer = await SCENARIOS["repaired_before_retry"](tmp_path / "newer")
    newer_events = [{**event, "ts": float(event["ts"]) + 1000.0} for event in newer.events]
    events = [*older.events, *newer_events]

    packets = sample_packets(events, n=2)
    assert [p["outcome"]["status"] for p in packets] == ["closed", "recovered"]
    assert packets[0]["outcome"]["resolution"] == "healthy_before_retry"

    assert [p["incident_id"] for p in sample_packets(events, n=1)] == [packets[0]["incident_id"]]


@pytest.mark.asyncio
async def test_review_sample_cli_exit_codes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = await SCENARIOS["full_loop_success"](tmp_path)
    log = _write_jsonl(tmp_path / "events.jsonl", run.events)

    assert sample_main(["prog", "--n", "1", "--events", str(log)]) == 0
    out = capsys.readouterr().out
    assert "# Review Packet:" in out

    assert sample_main(["prog", "--n", "0", "--events", str(log)]) == 2
    capsys.readouterr()

    assert sample_main(["prog", "--events", str(tmp_path / "missing.jsonl")]) == 2
    capsys.readouterr()

    log_without_decision = _write_jsonl(
        tmp_path / "no-decision.jsonl",
        [{"ts": 1.0, "event_type": "incident.started", "payload": {"incident_id": "inc-1"}}],
    )
    assert sample_main(["prog", "--events", str(log_without_decision)]) == 1
    assert "no incidents with decision traces found" in capsys.readouterr().err

