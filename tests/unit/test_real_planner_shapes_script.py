from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.phase8


def test_real_planner_shapes_script_uses_temp_copy_and_safe_stdout() -> None:
    script = Path("scripts/real_planner_shapes.py")
    source = script.read_text(encoding="utf-8")

    assert "TemporaryDirectory" in source
    assert "copytree(baseline_app_root, app_root)" in source

    # Script output is an artifact path only; no env/key dumps.
    assert "real-planner-shapes report=" in source
    print_lines = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith("print(")
    ]
    assert print_lines
    assert all("OPENAI_API_KEY" not in line for line in print_lines)
    assert all("MI_OPENAI_API_KEY" not in line for line in print_lines)
    assert all("MI_LLM_" not in line for line in print_lines)


def test_real_planner_shapes_script_defaults_s01_to_s10() -> None:
    script = Path("scripts/real_planner_shapes.py")
    source = script.read_text(encoding="utf-8")
    assert "S01,S02,S03,S04,S05,S06,S07,S08,S09,S10" in source
