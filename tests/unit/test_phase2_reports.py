from __future__ import annotations

import json
from pathlib import Path

import pytest

from mi_agent.eval.replay import ReplayLLMClient
from mi_agent.eval.reports import write_eval_reports
from mi_agent.eval.runner import EvalRunConfig, run_replay_eval

pytestmark = pytest.mark.phase2


def test_write_eval_reports_emits_json_and_markdown(tmp_path: Path) -> None:
    replay_file = tmp_path / "replay.json"
    replay_file.write_text(json.dumps([{"response": "plan"}] * 300), encoding="utf-8")

    summary = run_replay_eval(
        EvalRunConfig(
            seeds=1,
            curriculum="ordered",
            held_out_scenarios=frozenset({"S18"}),
            held_out_variants=True,
            drift=True,
            shuffle_seed=3,
            base_seed=7,
        ),
        ReplayLLMClient(replay_file),
    )

    report_json, report_md = write_eval_reports(summary, tmp_path / "reports", run_id="phase2")

    assert report_json.exists()
    assert report_md.exists()

    payload = json.loads(report_json.read_text(encoding="utf-8"))
    expected_keys = {
        "success_rate",
        "mttr_seconds",
        "actions_per_incident",
        "rollback_rate",
        "last_resort_rate",
        "data_loss_rate",
        "llm_calls_total",
        "llm_tokens_total",
        "diagnosis_accuracy",
        "verifier_oracle_disagreement_rate",
    }
    assert expected_keys.issubset(payload.keys())

    markdown = report_md.read_text(encoding="utf-8")
    assert "# MI Eval Report" in markdown
    assert "success_rate" in markdown
    assert "verifier_oracle_disagreement_rate" in markdown

