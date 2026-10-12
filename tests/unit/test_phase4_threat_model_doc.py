from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.phase4


def test_threat_model_doc_exists_and_covers_core_controls() -> None:
    doc_path = Path("docs/THREAT_MODEL.md")
    assert doc_path.exists()

    text = doc_path.read_text(encoding="utf-8")
    lowered = text.lower()

    assert "risk tier" in lowered
    assert "pre-shield" in lowered
    assert "post-shield" in lowered
    assert "budget" in lowered
    assert "degraded mode" in lowered
    assert "red-team" in lowered

