from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mi_agent.eval.ope import load_decision_log
from mi_agent.eval.reports import _jsonify
from mi_agent.policy.decision_log import DecisionLogEntry
from mi_agent.skills.model import SkillRecord


def build_dashboard_views(
    *,
    latest_eval_summary: dict[str, Any] | None,
    decision_entries: list[DecisionLogEntry],
    skills: list[SkillRecord],
) -> dict[str, Any]:
    learning_curve = _coerce_curve(latest_eval_summary)
    per_scenario = _coerce_scenario_results(latest_eval_summary)
    lifecycle = _skill_lifecycle_counts(skills)
    exploration_rate = _exploration_rate(decision_entries)

    last_resort_value: object = (
        latest_eval_summary.get("last_resort_rate")
        if isinstance(latest_eval_summary, dict)
        else None
    )
    last_resort_rate = (
        float(last_resort_value)
        if isinstance(last_resort_value, (int, float))
        else 0.0
    )

    return {
        "learning_curve": learning_curve,
        "per_scenario_results": per_scenario,
        "skill_lifecycle": lifecycle,
        "exploration_rate": exploration_rate,
        "last_resort_rate": last_resort_rate,
        "decision_count": len(decision_entries),
    }


def generate_phase8_report(
    *,
    experiments_json: Path,
    output_dir: Path,
    run_id: str,
    decision_log: Path,
    skills_json: Path,
    eval_json: Path | None = None,
) -> tuple[dict[str, Any], Path, Path]:
    experiments_payload = _load_json_file(experiments_json)
    eval_payload = _load_json_file(eval_json) if eval_json is not None else None
    summary_for_views = dict(experiments_payload)
    if eval_payload:
        summary_for_views.update(eval_payload)

    entries = load_decision_log(decision_log)
    skills = _load_skills(skills_json)
    dashboard_views = build_dashboard_views(
        latest_eval_summary=summary_for_views,
        decision_entries=entries,
        skills=skills,
    )

    payload = {
        "schema_version": "v1",
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "experiments": experiments_payload,
        "dashboard_views": dashboard_views,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{run_id}.json"
    markdown_path = output_dir / f"{run_id}.md"
    json_path.write_text(json.dumps(_jsonify(payload), indent=2, sort_keys=True), encoding="utf-8")

    lines = [
        "# MI v2 Phase 8 Report",
        "",
        f"run_id: {run_id}",
        "",
        "## Dashboard Views",
    ]
    lines.append(
        f"- learning_curve_points: {len(dashboard_views['learning_curve'])}"
    )
    lines.append(
        f"- per_scenario_results: {len(dashboard_views['per_scenario_results'])}"
    )
    lines.append(f"- skill_lifecycle: {dashboard_views['skill_lifecycle']}")
    lines.append(f"- exploration_rate: {dashboard_views['exploration_rate']}")
    lines.append(f"- last_resort_rate: {dashboard_views['last_resort_rate']}")

    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload, json_path, markdown_path


def _load_json_file(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _load_skills(path: Path) -> list[SkillRecord]:
    if not path.exists():
        return []
    try:
        payload = _load_skills_payload(path)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(payload, list):
        return []

    skills: list[SkillRecord] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            skills.append(SkillRecord.model_validate(item))
        except Exception:
            continue
    return skills


def _load_skills_payload(path: Path) -> object:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() != ".md":
        return json.loads(text)

    begin = "<!-- MI_SKILLS_JSON_BEGIN -->"
    end = "<!-- MI_SKILLS_JSON_END -->"
    begin_index = text.find(begin)
    end_index = text.find(end)
    if begin_index == -1 or end_index == -1 or end_index <= begin_index:
        return json.loads(text)

    block = text[begin_index + len(begin) : end_index].strip()
    if block.startswith("```"):
        lines = block.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        block = "\n".join(lines).strip()
    return json.loads(block)


def _coerce_curve(summary: dict[str, Any] | None) -> list[dict[str, float]]:
    if not isinstance(summary, dict):
        return []
    raw = summary.get("learning_curve")
    if not isinstance(raw, list):
        return []

    curve: list[dict[str, float]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        episode = item.get("episode")
        success_rate = item.get("success_rate")
        if isinstance(episode, (int, float)) and isinstance(success_rate, (int, float)):
            curve.append({"episode": float(episode), "success_rate": float(success_rate)})
    return curve


def _coerce_scenario_results(summary: dict[str, Any] | None) -> dict[str, float]:
    if not isinstance(summary, dict):
        return {}
    raw = summary.get("scenario_success_rate")
    if not isinstance(raw, dict):
        return {}
    results: dict[str, float] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, (int, float)):
            results[key] = float(value)
    return results


def _skill_lifecycle_counts(skills: list[SkillRecord]) -> dict[str, int]:
    counts = {
        "candidate": 0,
        "canary": 0,
        "trusted": 0,
        "deprecated": 0,
    }
    for skill in skills:
        counts[skill.lifecycle_state] = counts.get(skill.lifecycle_state, 0) + 1
    return counts


def _exploration_rate(entries: list[DecisionLogEntry]) -> float:
    if not entries:
        return 0.0
    exploration_mass = 0.0
    for entry in entries:
        if not entry.propensities:
            continue
        best_probability = max(entry.propensities.values())
        exploration_mass += max(0.0, 1.0 - best_probability)
    return exploration_mass / len(entries)
