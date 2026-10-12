from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.phase8


def test_phase8_required_docs_exist() -> None:
    assert Path("docs/THREAT_MODEL.md").exists()
    assert Path("docs/RESULTS.md").exists()
    assert Path("docs/ARCHITECTURE.md").exists()
    assert Path("README.md").exists()


def test_phase8_results_document_marks_replay_mode_and_unsolved_scenarios() -> None:
    text = Path("docs/RESULTS.md").read_text(encoding="utf-8")

    assert "replay-only" in text.lower()
    assert "S02" in text
    assert "S03" in text


def test_phase8_architecture_doc_mentions_dashboard_view_contract() -> None:
    text = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")

    assert "/dashboard/views" in text
    assert "learning_curve" in text
    assert "per_scenario_results" in text
    assert "exploration_rate" in text
    assert "last_resort_rate" in text


def test_readme_lists_v2_limitations_with_unsolved_scenarios() -> None:
    text = Path("README.md").read_text(encoding="utf-8")

    assert "Current limitations (MI v2 replay run)" in text
    assert "S02" in text
    assert "S03" in text

