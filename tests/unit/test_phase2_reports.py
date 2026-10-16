from __future__ import annotations

import json
from pathlib import Path

import pytest

from mi_agent.eval.reports import write_eval_reports
from mi_agent.eval.runner import EvalRunConfig, run_live_eval_sync

pytestmark = pytest.mark.phase2


def test_write_eval_reports_serializes_not_run_summary(tmp_path: Path, test_settings) -> None:
    summary = run_live_eval_sync(
        EvalRunConfig(
            seeds=1,
            curriculum="ordered",
            held_out_scenarios=frozenset(),
            held_out_variants=False,
            drift=False,
            shuffle_seed=17,
            base_seed=7,
            execute_live=False,
        ),
        test_settings,
    )

    json_path, markdown_path = write_eval_reports(summary, tmp_path, run_id="phase2")

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["live_run_executed"] is False
    assert markdown_path.exists()
