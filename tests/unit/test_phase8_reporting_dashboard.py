from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mi_agent.api.server import create_api
from mi_agent.eval import cli
from mi_agent.eval.reporting import generate_phase8_report
from mi_agent.llm.client import FakeLLMClient
from mi_agent.policy.decision_log import DecisionLogEntry, RewardComponents
from mi_agent.runtime import build_agent
from mi_agent.skills.library import SkillLibrary
from mi_agent.skills.model import SkillRecord, SkillStep

pytestmark = pytest.mark.phase8


def _decision_entry(
    *,
    incident_id: str,
    chosen_action: str,
    probability: float,
) -> DecisionLogEntry:
    return DecisionLogEntry(
        incident_id=incident_id,
        failure_type="db_file_missing",
        state_features={"probe_fingerprint": "abc", "attempt": "1"},
        candidate_set=["apply_schema", "restart_service"],
        chosen_action=chosen_action,
        propensities={"apply_schema": probability, "restart_service": 1.0 - probability},
        policy_version="v1-tabular",
        verifier_healthy=True,
        reward_components=RewardComponents(net_reward=1.0),
    )


def _skill(name: str, lifecycle_state: str) -> SkillRecord:
    return SkillRecord(
        name=name,
        description="recover",
        failure_type="db_file_missing",
        trigger_fingerprint={"probes": "missing"},
        steps=[
            SkillStep(
                primitive_id="apply_schema",
                parameters={"db_path": "data/app.db", "schema_path": "schema.sql"},
            )
        ],
        expected_postcondition="healthy",
        lifecycle_state=lifecycle_state,
    )


def test_phase8_report_generator_outputs_dashboard_views(tmp_path: Path) -> None:
    experiments_json = tmp_path / "experiments.json"
    experiments_json.write_text(
        json.dumps(
            {
                "learning_curve": [
                    {"episode": 1.0, "success_rate": 0.6},
                    {"episode": 2.0, "success_rate": 0.8},
                ]
            }
        ),
        encoding="utf-8",
    )

    eval_json = tmp_path / "eval.json"
    eval_json.write_text(
        json.dumps(
            {
                "scenario_success_rate": {"S01": 1.0, "S18": 0.9},
                "last_resort_rate": 0.05,
                "learning_curve": [{"episode": 1.0, "success_rate": 0.7}],
            }
        ),
        encoding="utf-8",
    )

    decision_log = tmp_path / "decision_log.jsonl"
    decision_entries = [
        _decision_entry(incident_id="inc-1", chosen_action="apply_schema", probability=0.9),
        _decision_entry(incident_id="inc-2", chosen_action="restart_service", probability=0.7),
    ]
    decision_log.write_text(
        "\n".join(json.dumps(item.model_dump(mode="json")) for item in decision_entries) + "\n",
        encoding="utf-8",
    )

    skills_json = tmp_path / "skills.json"
    skills_json.write_text(
        json.dumps(
            [
                _skill("candidate-skill", "candidate").model_dump(mode="json"),
                _skill("trusted-skill", "trusted").model_dump(mode="json"),
            ]
        ),
        encoding="utf-8",
    )

    payload, json_path, markdown_path = generate_phase8_report(
        experiments_json=experiments_json,
        output_dir=tmp_path / "reports",
        run_id="phase8_report",
        decision_log=decision_log,
        skills_json=skills_json,
        eval_json=eval_json,
    )

    assert json_path.exists()
    assert markdown_path.exists()
    dashboard_views = payload["dashboard_views"]
    assert dashboard_views["learning_curve"]
    assert dashboard_views["per_scenario_results"]["S18"] == 0.9
    assert dashboard_views["skill_lifecycle"]["candidate"] == 1
    assert dashboard_views["skill_lifecycle"]["trusted"] == 1
    assert dashboard_views["exploration_rate"] > 0
    assert dashboard_views["last_resort_rate"] == 0.05


def test_phase8_dashboard_views_route_exposes_required_panels(test_settings) -> None:
    agent, context = build_agent(test_settings, FakeLLMClient())

    async def _noop() -> None:
        return None

    agent.start = _noop  # type: ignore[method-assign]
    agent.stop = _noop  # type: ignore[method-assign]

    context.decision_log.append(
        _decision_entry(incident_id="inc-1", chosen_action="apply_schema", probability=0.85)
    )
    context.skill_library.register(_skill("skill-a", "candidate"))
    context.skill_library.register(_skill("skill-b", "trusted"))

    eval_dir = test_settings.state_dir / "eval_runs"
    eval_dir.mkdir(parents=True, exist_ok=True)
    (eval_dir / "run-1.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "learning_curve": [{"episode": 1.0, "success_rate": 0.8}],
                "scenario_success_rate": {"S01": 1.0, "S18": 0.95},
                "last_resort_rate": 0.02,
            }
        ),
        encoding="utf-8",
    )

    app = create_api(context, agent)
    with TestClient(app) as client:
        response = client.get("/dashboard/views")

    assert response.status_code == 200
    payload = response.json()
    assert "learning_curve" in payload
    assert "per_scenario_results" in payload
    assert "skill_lifecycle" in payload
    assert "exploration_rate" in payload
    assert "last_resort_rate" in payload
    assert payload["per_scenario_results"]["S18"] == 0.95
    assert payload["skill_lifecycle"]["trusted"] == 1


def test_phase8_dashboard_html_includes_new_view_panels() -> None:
    html = Path("dashboard/index.html").read_text(encoding="utf-8")

    assert "learning-curve" in html
    assert "per-scenario-results" in html
    assert "skill-lifecycle" in html
    assert "exploration-rate" in html
    assert "last-resort-rate" in html


def test_phase8_report_cli_writes_report_files(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    experiments_json = tmp_path / "experiments.json"
    experiments_json.write_text(
        json.dumps({"learning_curve": [{"episode": 1.0, "success_rate": 0.75}]}),
        encoding="utf-8",
    )

    decision_log = tmp_path / "decision_log.jsonl"
    entry_payload = _decision_entry(
        incident_id="inc-1",
        chosen_action="apply_schema",
        probability=0.8,
    ).model_dump(mode="json")
    decision_log.write_text(
        json.dumps(entry_payload) + "\n",
        encoding="utf-8",
    )

    skills_json = tmp_path / "skills.json"
    skills_json.write_text(
        json.dumps([_skill("skill-a", "candidate").model_dump(mode="json")]),
        encoding="utf-8",
    )

    output_dir = tmp_path / "reports"
    exit_code = cli.main(
        [
            "report",
            "--experiments-json",
            str(experiments_json),
            "--decision-log",
            str(decision_log),
            "--skills-json",
            str(skills_json),
            "--output-dir",
            str(output_dir),
            "--run-id",
            "phase8_dashboard",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "mi-eval report" in captured.out
    assert (output_dir / "phase8_dashboard.json").exists()
    assert (output_dir / "phase8_dashboard.md").exists()


def test_phase8_report_generator_accepts_markdown_skill_store(tmp_path: Path) -> None:
    experiments_json = tmp_path / "experiments.json"
    experiments_json.write_text(
        json.dumps({"learning_curve": [{"episode": 1.0, "success_rate": 0.75}]}),
        encoding="utf-8",
    )

    decision_log = tmp_path / "decision_log.jsonl"
    entry_payload = _decision_entry(
        incident_id="inc-1",
        chosen_action="apply_schema",
        probability=0.8,
    ).model_dump(mode="json")
    decision_log.write_text(
        json.dumps(entry_payload) + "\n",
        encoding="utf-8",
    )

    skills_md = tmp_path / "skills.md"
    library = SkillLibrary(skills_md)
    library.register(_skill("skill-md", "trusted"))

    payload, _, _ = generate_phase8_report(
        experiments_json=experiments_json,
        output_dir=tmp_path / "reports",
        run_id="phase8_md_skill_report",
        decision_log=decision_log,
        skills_json=skills_md,
    )

    assert payload["dashboard_views"]["skill_lifecycle"]["trusted"] == 1
