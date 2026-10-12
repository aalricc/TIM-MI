from __future__ import annotations

import json
from pathlib import Path

import pytest

from mi_agent.eval import cli
from mi_agent.eval.promotion import evaluate_promotion
from mi_agent.policy.decision_log import DecisionLogEntry, RewardComponents

pytestmark = pytest.mark.phase3


def _entry(*, reward: float, policy_version: str) -> DecisionLogEntry:
    return DecisionLogEntry(
        incident_id="inc-1",
        failure_type="db_file_missing",
        state_features={"probe_fingerprint": "fp"},
        candidate_set=["a", "b"],
        chosen_action="a",
        propensities={"a": 0.5, "b": 0.5},
        policy_version=policy_version,
        verifier_healthy=reward > 0,
        reward_components=RewardComponents(net_reward=reward),
    )


def test_promotion_rule_requires_confident_improvement() -> None:
    incumbent = {"dr": 0.40, "dr_se": 0.02}
    candidate = {"dr": 0.55, "dr_se": 0.02}

    decision = evaluate_promotion(incumbent, candidate, metric="dr")

    assert decision["promote"] is True


def test_promotion_rule_rejects_overlapping_confidence_bounds() -> None:
    incumbent = {"dr": 0.40, "dr_se": 0.03}
    candidate = {"dr": 0.43, "dr_se": 0.03}

    decision = evaluate_promotion(incumbent, candidate, metric="dr")

    assert decision["promote"] is False


def test_mi_eval_promote_command(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    incumbent_log = tmp_path / "incumbent.jsonl"
    candidate_log = tmp_path / "candidate.jsonl"

    incumbent_entries = [_entry(reward=0.2, policy_version="inc") for _ in range(40)]
    candidate_entries = [_entry(reward=0.8, policy_version="cand") for _ in range(40)]

    incumbent_log.write_text(
        "\n".join(json.dumps(entry.model_dump(mode="json")) for entry in incumbent_entries) + "\n",
        encoding="utf-8",
    )
    candidate_log.write_text(
        "\n".join(json.dumps(entry.model_dump(mode="json")) for entry in candidate_entries) + "\n",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "promote",
            "--incumbent-log",
            str(incumbent_log),
            "--candidate-log",
            str(candidate_log),
            "--target-policy",
            "uniform",
            "--metric",
            "dr",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "promote" in captured.out

