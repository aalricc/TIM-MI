from __future__ import annotations

import json
from pathlib import Path

import pytest

from mi_agent.eval import cli
from mi_agent.eval.audit import export_audit_trail
from mi_agent.events.bus import EventBus
from mi_agent.events.models import Event
from mi_agent.policy.decision_log import DecisionLogEntry, RewardComponents

pytestmark = pytest.mark.phase8


def _decision_entry() -> DecisionLogEntry:
    return DecisionLogEntry(
        incident_id="inc-1",
        failure_type="db_file_missing",
        state_features={"probe_fingerprint": "abc", "attempt": "1"},
        candidate_set=["apply_schema"],
        chosen_action="apply_schema",
        propensities={"apply_schema": 1.0},
        policy_version="v1-tabular",
        verifier_healthy=True,
        reward_components=RewardComponents(net_reward=1.0),
    )


@pytest.mark.asyncio()
async def test_phase8_event_bus_persists_event_log(tmp_path: Path) -> None:
    event_log = tmp_path / "events.jsonl"
    bus = EventBus(event_log)

    await bus.publish(Event(event_type="tool.executed", payload={"action": "apply_schema"}))
    await bus.publish(Event(event_type="verifier.result", payload={"healthy": True}))

    lines = event_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["event_type"] == "tool.executed"
    assert second["event_type"] == "verifier.result"


def test_phase8_export_audit_trail_contains_decisions_actions_and_verifier(tmp_path: Path) -> None:
    decision_log = tmp_path / "decision_log.jsonl"
    decision_log.write_text(
        json.dumps(_decision_entry().model_dump(mode="json")) + "\n",
        encoding="utf-8",
    )

    event_log = tmp_path / "events.jsonl"
    event_log.write_text(
        "\n".join(
            [
                json.dumps({"event_type": "tool.executed", "payload": {"action": "apply_schema"}}),
                json.dumps({"event_type": "verifier.result", "payload": {"healthy": True}}),
                json.dumps(
                    {
                        "event_type": "agent.action_executed",
                        "payload": {"action_key": "apply_schema"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    payload, json_path, jsonl_path = export_audit_trail(
        decision_log_path=decision_log,
        event_log_path=event_log,
        output_dir=tmp_path / "audit",
        run_id="phase8_audit",
    )

    assert json_path.exists()
    assert jsonl_path.exists()
    assert payload["counts"]["decisions"] == 1
    assert payload["counts"]["actions"] == 2
    assert payload["counts"]["verifier_results"] == 1


def test_phase8_audit_export_cli_writes_files(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    decision_log = tmp_path / "decision_log.jsonl"
    decision_log.write_text(
        json.dumps(_decision_entry().model_dump(mode="json")) + "\n",
        encoding="utf-8",
    )

    event_log = tmp_path / "events.jsonl"
    event_log.write_text(
        json.dumps({"event_type": "verifier.result", "payload": {"healthy": True}}) + "\n",
        encoding="utf-8",
    )

    output_dir = tmp_path / "audit"
    exit_code = cli.main(
        [
            "audit-export",
            "--decision-log",
            str(decision_log),
            "--event-log",
            str(event_log),
            "--output-dir",
            str(output_dir),
            "--run-id",
            "phase8_audit",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "mi-eval audit-export" in captured.out
    assert (output_dir / "phase8_audit.json").exists()
    assert (output_dir / "phase8_audit.jsonl").exists()
