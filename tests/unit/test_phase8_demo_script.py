from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.phase8


def test_phase8_demo_script_exists_and_is_executable() -> None:
    script = Path("scripts/v2_demo.sh")
    assert script.exists()
    assert script.stat().st_mode & 0o111


def test_phase8_demo_script_generates_expected_artifacts(tmp_path: Path) -> None:
    output_dir = tmp_path / "demo"

    result = subprocess.run(
        ["bash", "scripts/v2_demo.sh", str(output_dir)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "v2 demo complete" in result.stdout
    assert "pass1_live=" in result.stdout
    assert "pass2_live=" in result.stdout
    assert "experiments_live=" in result.stdout

    pass1 = output_dir / "phase8_demo_pass1.json"
    pass2 = output_dir / "phase8_demo_pass2.json"
    experiments = output_dir / "phase8_demo_experiments.json"
    assert pass1.exists()
    assert pass2.exists()
    assert experiments.exists()

    payload = json.loads(experiments.read_text(encoding="utf-8"))
    assert payload["summary"]["live_run_executed"] is False

