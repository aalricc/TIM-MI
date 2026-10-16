from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.phase8


def _pythonpath_env() -> dict[str, str]:
    env = dict(os.environ)
    current = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = "src" if current == "" else f"src:{current}"
    return env


def _pythonpath_env_without_llm_keys() -> dict[str, str]:
    env = _pythonpath_env()
    env.pop("OPENAI_API_KEY", None)
    env.pop("MI_OPENAI_API_KEY", None)
    return env


def test_real_planner_shapes_rescore_uses_configurable_acceptable_set(tmp_path: Path) -> None:
    input_report = {
        "mode": "real_planner_shapes",
        "scenarios": [
            {
                "scenario_id": "S01",
                "runs": [
                    {
                        "run_index": 1,
                        "plan_shapes": [
                            {
                                "plan_name": "a",
                                "action_keys": ["read_app_log", "request_backup_restore"],
                                "plan_class": "read_then_mutate",
                                "first_executable_action": "request_backup_restore",
                                "read_action_keys": ["read_app_log"],
                                "mutating_action_keys": ["request_backup_restore"],
                                "mutating_steps_with_read_output_refs": [],
                            }
                        ],
                    },
                    {
                        "run_index": 2,
                        "plan_shapes": [
                            {
                                "plan_name": "b",
                                "action_keys": ["read_app_log", "restart_service"],
                                "plan_class": "read_then_mutate",
                                "first_executable_action": "restart_service",
                                "read_action_keys": ["read_app_log"],
                                "mutating_action_keys": ["restart_service"],
                                "mutating_steps_with_read_output_refs": [],
                            }
                        ],
                    },
                    {
                        "run_index": 3,
                        "plan_shapes": [
                            {
                                "plan_name": "c",
                                "action_keys": ["reindex_db"],
                                "plan_class": "mutate_first",
                                "first_executable_action": "reindex_db",
                                "read_action_keys": [],
                                "mutating_action_keys": ["reindex_db"],
                                "mutating_steps_with_read_output_refs": [],
                            }
                        ],
                    },
                ],
            }
        ],
    }
    acceptable = {
        "Delete DB file": ["request_backup_restore", "restart_service"],
    }

    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    acceptable_path = tmp_path / "acceptable.json"
    input_path.write_text(json.dumps(input_report), encoding="utf-8")
    acceptable_path.write_text(json.dumps(acceptable), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "scripts/real_planner_shapes.py",
            "--mode",
            "rescore",
            "--input-report",
            str(input_path),
            "--acceptable-remediations",
            str(acceptable_path),
            "--output",
            str(output_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=_pythonpath_env(),
    )

    assert "real-planner-shapes report=" in result.stdout
    rescored = json.loads(output_path.read_text(encoding="utf-8"))

    aggregate = rescored["aggregate"]
    assert aggregate["scenario_count"] == 1
    assert aggregate["total_plans"] == 3
    assert aggregate["scenario_match_breakdown"]["S01"] == {
        "top_ranked_matched_runs": 2,
        "any_plan_matched_runs": 2,
        "total_runs": 3,
    }
    assert aggregate["top_ranked_match"] == {
        "matched_runs": 2,
        "total_runs": 3,
        "rate": pytest.approx(2 / 3),
    }
    assert aggregate["any_plan_match"] == {
        "matched_runs": 2,
        "total_runs": 3,
        "rate": pytest.approx(2 / 3),
    }
    assert aggregate["first_mutating_match_rate"] == pytest.approx(2 / 3)

    shapes = rescored["scenarios"][0]["runs"][0]["plan_shapes"]
    assert shapes[0]["matches_acceptable_remediation"] is True
    assert shapes[0]["matches_expected_remediation"] is True

    shapes = rescored["scenarios"][0]["runs"][1]["plan_shapes"]
    assert shapes[0]["matches_acceptable_remediation"] is True

    shapes = rescored["scenarios"][0]["runs"][2]["plan_shapes"]
    assert shapes[0]["matches_acceptable_remediation"] is False


def test_real_planner_shapes_full_loop_compare_mode_rejects_zero_runs(tmp_path: Path) -> None:
    output_path = tmp_path / "full_loop_compare.json"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/real_planner_shapes.py",
            "--mode",
            "full_loop_compare",
            "--runs",
            "0",
            "--scenarios",
            "S01,S02",
            "--baseline-app-root",
            "target/app",
            "--output",
            str(output_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_pythonpath_env(),
    )

    assert result.returncode != 0
    assert "full_loop_compare requires --runs >= 1" in result.stderr
    assert output_path.exists() is False


def test_real_planner_shapes_full_loop_compare_aborts_when_planner_unreachable(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "full_loop_compare_unreachable.json"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/real_planner_shapes.py",
            "--mode",
            "full_loop_compare",
            "--runs",
            "1",
            "--scenarios",
            "S01",
            "--baseline-app-root",
            "target/app",
            "--output",
            str(output_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_pythonpath_env_without_llm_keys(),
    )

    assert result.returncode != 0
    assert "planner unreachable or bypassed" in result.stderr
    assert "scenario=S01" in result.stderr
    assert output_path.exists() is False


def test_real_planner_shapes_full_loop_outcome_compare_aborts_when_planner_unreachable(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "full_loop_outcome_compare_unreachable.json"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/real_planner_shapes.py",
            "--mode",
            "full_loop_outcome_compare",
            "--runs",
            "1",
            "--scenarios",
            "S01",
            "--baseline-app-root",
            "target/app",
            "--output",
            str(output_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_pythonpath_env_without_llm_keys(),
    )

    assert result.returncode != 0
    assert "planner unreachable or bypassed" in result.stderr
    assert "scenario=S01" in result.stderr
    assert output_path.exists() is False


def test_real_planner_shapes_analyze_top_rank_reports_first_acceptable_rank(
    tmp_path: Path,
) -> None:
    input_report = {
        "mode": "real_planner_shapes_rescored",
        "scenarios": [
            {
                "scenario_id": "S01",
                "acceptable_first_mutating_actions": ["request_backup_restore"],
                "runs": [
                    {
                        "run_index": 1,
                        "plan_shapes": [
                            {
                                "plan_name": "p1",
                                "action_keys": ["read_app_log", "restart_service"],
                                "first_executable_action": "restart_service",
                            },
                            {
                                "plan_name": "p2",
                                "action_keys": ["list_backups", "request_backup_restore"],
                                "first_executable_action": "request_backup_restore",
                            },
                        ],
                    }
                ],
            }
        ],
    }

    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    input_path.write_text(json.dumps(input_report), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "scripts/real_planner_shapes.py",
            "--mode",
            "analyze_top_rank",
            "--input-report",
            str(input_path),
            "--output",
            str(output_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=_pythonpath_env(),
    )

    assert "real-planner-shapes report=" in result.stdout
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["summary"]["top_ranked_match"] == {
        "matched_runs": 0,
        "total_runs": 1,
        "rate": 0.0,
    }
    assert payload["summary"]["any_plan_match"] == {
        "matched_runs": 1,
        "total_runs": 1,
        "rate": 1.0,
    }
    run = payload["runs"][0]
    assert run["first_acceptable_rank"] == 2
    assert [item["first_executable_action"] for item in run["plan_order"]] == [
        "restart_service",
        "request_backup_restore",
    ]
