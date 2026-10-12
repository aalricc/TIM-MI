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
    replay_file = tmp_path / "replay.json"

    result = subprocess.run(
        ["bash", "scripts/v2_demo.sh", str(output_dir), str(replay_file)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "v2 demo complete" in result.stdout
    assert "repeat_incident_speedup=" in result.stdout

    pass1 = output_dir / "phase8_demo_pass1.json"
    pass2 = output_dir / "phase8_demo_pass2.json"
    experiments = output_dir / "phase8_demo_experiments.json"
    assert pass1.exists()
    assert pass2.exists()
    assert experiments.exists()

    payload = json.loads(experiments.read_text(encoding="utf-8"))
    speedup = payload["repeat_incident"]["skill_reuse_speedup"]["learning_enabled"]
    assert speedup >= 0.0

