from __future__ import annotations

import re
import stat
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.phase0


def test_gate_script_exists_and_is_executable() -> None:
    gate = Path("scripts/v2_gate.sh")
    assert gate.exists()
    mode = gate.stat().st_mode
    assert mode & stat.S_IXUSR


def test_gate_script_includes_required_checks_in_order() -> None:
    gate = Path("scripts/v2_gate.sh").read_text(encoding="utf-8")
    expected = [
        'run_check "ruff check ." ruff check .',
        'run_check "mypy --strict src" mypy --strict src',
        'run_check "pytest -q -m integrity" pytest -q -m integrity',
        'run_check "pytest -q -m ${phase_marker}" pytest -q -m "$phase_marker"',
        'run_check "pytest -q" pytest -q',
        'run_check "mi-eval smoke --replay" mi-eval smoke --replay',
    ]

    index = -1
    for fragment in expected:
        pos = gate.find(fragment)
        assert pos > index, f"Missing or out-of-order gate step: {fragment}"
        index = pos

    assert "GATE PASS" in gate
    assert "GATE FAIL" in gate


def test_pytest_markers_include_integrity_and_phase_markers() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    markers = pyproject["tool"]["pytest"]["ini_options"]["markers"]
    assert any(marker.startswith("integrity:") for marker in markers)
    for phase in range(9):
        marker_key = f"phase{phase}:"
        assert any(marker.startswith(marker_key) for marker in markers)


def test_makefile_has_v2_gate_target() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")
    assert re.search(r"^\.PHONY:\s+v2-gate$", makefile, flags=re.MULTILINE)
    assert re.search(r"^v2-gate:\s*$", makefile, flags=re.MULTILINE)
    assert "./scripts/v2_gate.sh $(PHASE) $(ARGS)" in makefile
