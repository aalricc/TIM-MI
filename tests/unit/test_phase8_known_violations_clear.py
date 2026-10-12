from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.phase8


def test_phase8_known_violations_file_is_empty_of_violation_ids() -> None:
    content = Path("docs/v2/KNOWN_VIOLATIONS.md").read_text(encoding="utf-8")
    assert re.search(r"\bV2-\d{3}\b", content) is None

