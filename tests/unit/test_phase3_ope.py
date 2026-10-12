from __future__ import annotations

import json
from pathlib import Path

import pytest

from mi_agent.eval import cli
from mi_agent.eval.ope import estimate_off_policy
from mi_agent.policy.decision_log import DecisionLogEntry, RewardComponents

pytestmark = pytest.mark.phase3


def _entry(*, action: str, reward: float) -> DecisionLogEntry:
    return DecisionLogEntry(
        incident_id="inc-1",
        failure_type="db_file_missing",
        state_features={"probe_fingerprint": "p1"},
        candidate_set=["a", "b"],
        chosen_action=action,
        propensities={"a": 0.5, "b": 0.5},
        policy_version="v1-tabular",
        verifier_healthy=reward > 0,
        reward_components=RewardComponents(net_reward=reward),
    )


def test_ope_estimators_on_simple_two_action_log() -> None:
    entries = [_entry(action="a", reward=1.0), _entry(action="b", reward=0.0)]

    result = estimate_off_policy(entries, target_policy="uniform")

    assert result["count"] == 2
    assert result["ips"] == pytest.approx(0.5)
    assert result["snips"] == pytest.approx(0.5)
    assert result["dr"] == pytest.approx(0.5)


def test_mi_eval_ope_command_reads_decision_log(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    log_file = tmp_path / "decision_log.jsonl"
    entries = [_entry(action="a", reward=1.0), _entry(action="b", reward=0.0)]
    log_file.write_text(
        "\n".join(json.dumps(entry.model_dump(mode="json")) for entry in entries) + "\n",
        encoding="utf-8",
    )

    exit_code = cli.main(["ope", "--decision-log", str(log_file), "--target-policy", "uniform"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "ips" in captured.out
    assert "snips" in captured.out
    assert "dr" in captured.out

