from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path


def _jsonify(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, dict):
        return {key: _jsonify(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonify(item) for item in value]
    return value


def write_eval_reports(
    summary: dict[str, object],
    output_dir: Path,
    *,
    run_id: str,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{run_id}.json"
    markdown_path = output_dir / f"{run_id}.md"

    serialized = _jsonify(summary)
    json_path.write_text(json.dumps(serialized, indent=2, sort_keys=True), encoding="utf-8")

    lines = [
        "# MI Eval Report",
        "",
        f"run_id: {run_id}",
        "",
        "## Metrics",
    ]
    metric_order = (
        "episodes",
        "success_rate",
        "mttr_seconds",
        "actions_per_incident",
        "rollback_rate",
        "last_resort_rate",
        "data_loss_rate",
        "llm_calls_total",
        "llm_tokens_total",
        "diagnosis_accuracy",
        "verifier_oracle_disagreement_rate",
    )
    for key in metric_order:
        if key in summary:
            lines.append(f"- {key}: {summary[key]}")

    split_counts = summary.get("split_counts", {})
    if isinstance(split_counts, dict):
        lines.append("")
        lines.append("## Split Counts")
        for key, value in split_counts.items():
            lines.append(f"- {key}: {value}")

    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path
