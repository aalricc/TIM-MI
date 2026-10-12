#!/usr/bin/env python
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from shutil import copytree
from time import perf_counter
from typing import Any

from mi_agent.agent.diagnose import Diagnoser
from mi_agent.config import MISettings
from mi_agent.eval.injectors import InjectorContext, run_injector
from mi_agent.eval.scenarios import SCENARIO_BY_ID
from mi_agent.events.bus import EventBus
from mi_agent.llm.client import OpenAILLMClient
from mi_agent.policy.store import PolicyStore
from mi_agent.runtime import build_agent
from mi_agent.tools.executor import ToolExecutor

DEFAULT_SCENARIOS = "S01,S02,S03,S04,S05,S06,S07,S08,S09,S10"
_REFERENCE_TOKEN_PATTERNS = (
    re.compile(r"\$\{[^}]+\}"),
    re.compile(r"\{\{[^}]+\}\}"),
    re.compile(r"\b(step|output|result|observation|quoted_data)\b", re.IGNORECASE),
)


@dataclass(frozen=True, slots=True)
class PlanShape:
    plan_name: str
    action_keys: list[str]
    plan_class: str
    first_executable_action: str | None
    read_action_keys: list[str]
    mutating_action_keys: list[str]
    mutating_steps_with_read_output_refs: list[str]
    mutating_param_structure: list[dict[str, Any]]
    matches_acceptable_remediation: bool


class RecordingLLMClient:
    def __init__(self, inner: OpenAILLMClient) -> None:
        self._inner = inner
        self.plan_calls: list[list[Any]] = []

    @property
    def usage(self) -> Any:
        return self._inner.usage

    async def diagnose(
        self,
        *,
        failure_type_hint: str | None,
        sanitized_logs: list[str],
        probes: dict[str, Any],
        lessons: list[str],
    ) -> Any:
        return await self._inner.diagnose(
            failure_type_hint=failure_type_hint,
            sanitized_logs=sanitized_logs,
            probes=probes,
            lessons=lessons,
        )

    async def propose_plans(
        self,
        *,
        diagnosis: Any,
        available_actions: list[str],
        action_parameter_contracts: dict[str, Any],
        recalled_skills: list[str],
        failed_action_keys: list[str] = (),
        constraints: dict[str, Any] | None = None,
    ) -> list[Any]:
        plans = await self._inner.propose_plans(
            diagnosis=diagnosis,
            available_actions=available_actions,
            action_parameter_contracts=action_parameter_contracts,
            recalled_skills=recalled_skills,
            failed_action_keys=failed_action_keys,
            constraints=constraints,
        )
        self.plan_calls.append([plan.model_copy(deep=True) for plan in plans])
        return plans

    async def distill_skill(
        self,
        *,
        failure_type: str,
        episode_actions: list[tuple[str, dict[str, Any], bool]],
        trigger_fingerprint: dict[str, str],
    ) -> Any:
        return await self._inner.distill_skill(
            failure_type=failure_type,
            episode_actions=episode_actions,
            trigger_fingerprint=trigger_fingerprint,
        )

    async def match_skill(
        self,
        *,
        failure_type: str,
        trigger_fingerprint: dict[str, str],
        candidate_skills: list[str],
    ) -> Any:
        return await self._inner.match_skill(
            failure_type=failure_type,
            trigger_fingerprint=trigger_fingerprint,
            candidate_skills=candidate_skills,
        )

    async def write_lesson(self, *, failure_type: str, summary: str) -> str:
        return await self._inner.write_lesson(failure_type=failure_type, summary=summary)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Real planner analysis and readonly-replan eval")
    parser.add_argument(
        "--mode",
        choices=(
            "planner_shapes",
            "rescore",
            "full_loop_compare",
            "analyze_top_rank",
            "full_loop_outcome_compare",
            "validate_reference_routes",
        ),
        default="planner_shapes",
        help=(
            "planner_shapes (default), rescore, full_loop_compare, analyze_top_rank, "
            "full_loop_outcome_compare, or validate_reference_routes"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/eval/real_planner_shapes.json"),
        help="JSON output path",
    )
    parser.add_argument(
        "--input-report",
        type=Path,
        default=None,
        help="Input report path for --mode rescore",
    )
    parser.add_argument(
        "--acceptable-remediations",
        type=Path,
        default=None,
        help=(
            "Optional JSON map from scenario id OR injected fault string to array of acceptable "
            "mutating action ids"
        ),
    )
    parser.add_argument(
        "--scenarios",
        default=DEFAULT_SCENARIOS,
        help="Comma-separated scenario ids",
    )
    parser.add_argument("--runs", type=int, default=3, help="Planner calls/runs per scenario")
    parser.add_argument("--base-seed", type=int, default=7, help="Base injector seed")
    parser.add_argument(
        "--baseline-app-root",
        type=Path,
        default=None,
        help="Source app root to copy into temp sandboxes (default: MI app_root)",
    )
    return parser.parse_args()


def _parse_scenario_ids(raw: str) -> list[str]:
    scenario_ids = [item.strip() for item in raw.split(",") if item.strip()]
    if not scenario_ids:
        raise SystemExit("No scenarios supplied")
    unknown = [item for item in scenario_ids if item not in SCENARIO_BY_ID]
    if unknown:
        raise SystemExit(f"Unknown scenario ids: {', '.join(sorted(unknown))}")
    return scenario_ids


def _is_executable_step(tools: ToolExecutor, action_id: str) -> bool:
    risk_tier = tools.risk_tier(action_id)
    return (
        action_id == "wait"
        or tools.is_mutating(action_id)
        or risk_tier in {"brokered", "terminal"}
    )


def _default_acceptable_actions(tools: ToolExecutor, scenario_id: str) -> set[str]:
    scenario = SCENARIO_BY_ID[scenario_id]
    expected: set[str] = set()
    for route in scenario.reference_routes:
        for action_id in route:
            if _is_executable_step(tools, action_id):
                expected.add(action_id)
                break
    return expected


def _load_acceptable_remediations(path: Path | None) -> dict[str, set[str]]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("--acceptable-remediations must be a JSON object")

    normalized: dict[str, set[str]] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            raise SystemExit("--acceptable-remediations keys must be strings")
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise SystemExit(
                f"--acceptable-remediations[{key!r}] must be an array of action-id strings"
            )
        normalized[key] = {item.strip() for item in value if item.strip()}
    return normalized


def _resolve_acceptable_actions(
    *,
    tools: ToolExecutor,
    scenario_id: str,
    overrides: dict[str, set[str]],
) -> set[str]:
    scenario = SCENARIO_BY_ID[scenario_id]
    if scenario_id in overrides:
        return set(overrides[scenario_id])
    if scenario.injected_fault in overrides:
        return set(overrides[scenario.injected_fault])
    return _default_acceptable_actions(tools, scenario_id)


def _iter_scalar_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        collected: list[str] = []
        for child in value.values():
            collected.extend(_iter_scalar_strings(child))
        return collected
    if isinstance(value, list):
        collected: list[str] = []
        for child in value:
            collected.extend(_iter_scalar_strings(child))
        return collected
    return []


def _params_reference_prior_reads(parameters: dict[str, Any], read_action_ids: list[str]) -> bool:
    if not read_action_ids:
        return False
    strings = _iter_scalar_strings(parameters)
    if not strings:
        return False

    read_tokens = {token.lower() for token in read_action_ids}
    for text in strings:
        lowered = text.lower()
        if any(pattern.search(text) for pattern in _REFERENCE_TOKEN_PATTERNS):
            return True
        if any(token in lowered for token in read_tokens):
            return True
    return False


def _param_references_read(value: Any, read_action_ids: list[str]) -> bool:
    strings = _iter_scalar_strings(value)
    if not strings:
        return False
    read_tokens = {token.lower() for token in read_action_ids}
    for text in strings:
        lowered = text.lower()
        if any(pattern.search(text) for pattern in _REFERENCE_TOKEN_PATTERNS):
            return True
        if any(token in lowered for token in read_tokens):
            return True
    return False


def _referencing_parameter_keys(
    parameters: dict[str, Any],
    read_action_ids: list[str],
) -> list[str]:
    referenced: list[str] = []
    for key, value in parameters.items():
        if _param_references_read(value, read_action_ids):
            referenced.append(str(key))
    return sorted(referenced)


def _classify_plan_shape(
    *,
    tools: ToolExecutor,
    plan: Any,
    acceptable_actions: set[str],
) -> PlanShape:
    action_keys = [step.action_id for step in plan.steps]
    first_executable_index: int | None = None
    first_executable_action: str | None = None
    read_action_keys: list[str] = []
    mutating_action_keys: list[str] = []
    mutating_steps_with_read_output_refs: list[str] = []
    mutating_param_structure: list[dict[str, Any]] = []

    for index, step in enumerate(plan.steps):
        if _is_executable_step(tools, step.action_id):
            first_executable_index = index
            first_executable_action = step.action_id
            break

    if first_executable_index is None:
        plan_class = "read_only_throughout"
        read_action_keys = action_keys
    elif first_executable_index == 0:
        plan_class = "mutate_first"
    else:
        plan_class = "read_then_mutate"
        read_action_keys = action_keys[:first_executable_index]

    for step in plan.steps:
        if _is_executable_step(tools, step.action_id):
            mutating_action_keys.append(step.action_id)
            referenced_keys = _referencing_parameter_keys(step.parameters, read_action_keys)
            mutating_param_structure.append(
                {
                    "action_id": step.action_id,
                    "parameter_keys": sorted(str(key) for key in step.parameters),
                    "referencing_parameter_keys": referenced_keys,
                    "references_read_outputs": bool(referenced_keys),
                }
            )
            if _params_reference_prior_reads(step.parameters, read_action_keys):
                mutating_steps_with_read_output_refs.append(step.action_id)

    return PlanShape(
        plan_name=str(getattr(plan, "name", "unnamed")),
        action_keys=action_keys,
        plan_class=plan_class,
        first_executable_action=first_executable_action,
        read_action_keys=read_action_keys,
        mutating_action_keys=mutating_action_keys,
        mutating_steps_with_read_output_refs=mutating_steps_with_read_output_refs,
        mutating_param_structure=mutating_param_structure,
        matches_acceptable_remediation=(
            first_executable_action in acceptable_actions
            if first_executable_action is not None
            else False
        ),
    )


def _serialize_shape(shape: PlanShape) -> dict[str, Any]:
    return {
        "plan_name": shape.plan_name,
        "action_keys": shape.action_keys,
        "plan_class": shape.plan_class,
        "first_executable_action": shape.first_executable_action,
        "read_action_keys": shape.read_action_keys,
        "mutating_action_keys": shape.mutating_action_keys,
        "mutating_steps_with_read_output_refs": shape.mutating_steps_with_read_output_refs,
        "mutating_param_structure": shape.mutating_param_structure,
        "matches_acceptable_remediation": shape.matches_acceptable_remediation,
        "matches_expected_remediation": shape.matches_acceptable_remediation,
    }


def _enrich_shape_from_action_ids(shape: dict[str, Any], tools: ToolExecutor) -> None:
    action_keys = shape.get("action_keys")
    if not isinstance(action_keys, list) or not all(isinstance(item, str) for item in action_keys):
        return

    first_executable_index: int | None = None
    for index, action_id in enumerate(action_keys):
        if _is_executable_step(tools, action_id):
            first_executable_index = index
            break

    if first_executable_index is None:
        read_action_keys = list(action_keys)
    else:
        read_action_keys = list(action_keys[:first_executable_index])

    mutating_action_keys = [
        action_id for action_id in action_keys if _is_executable_step(tools, action_id)
    ]

    shape.setdefault("read_action_keys", read_action_keys)
    shape.setdefault("mutating_action_keys", mutating_action_keys)
    shape.setdefault("mutating_steps_with_read_output_refs", [])
    shape.setdefault("mutating_param_structure", [])


def _summarize_planner_shapes(report: dict[str, Any]) -> dict[str, Any]:
    scenarios = report.get("scenarios", [])
    shape_counts: Counter[str] = Counter()
    matched_plans = 0
    total_runs = 0
    top_ranked_matched_runs = 0
    any_plan_matched_runs = 0
    total_plans = 0
    latency_values: list[float] = []
    read_then_mutate_plan_count = 0
    mutating_steps_with_read_refs = 0
    scenario_match_breakdown: dict[str, dict[str, int]] = {}
    scenario_action_breakdown: dict[str, dict[str, list[str]]] = {}

    for scenario_report in scenarios:
        scenario_id = str(scenario_report.get("scenario_id", ""))
        runs = scenario_report.get("runs", [])
        top_ranked_matched = 0
        any_plan_matched = 0
        read_actions: set[str] = set()
        mutating_actions: set[str] = set()
        for run in runs:
            total_runs += 1
            latency = run.get("latency_ms")
            if isinstance(latency, (int, float)):
                latency_values.append(float(latency))
            run_top_match = False
            run_matched = False
            plan_shapes = run.get("plan_shapes", [])
            if plan_shapes:
                first_shape = plan_shapes[0]
                run_top_match = bool(first_shape.get("matches_acceptable_remediation"))
                if run_top_match:
                    top_ranked_matched_runs += 1
                    top_ranked_matched += 1

            for shape in plan_shapes:
                plan_class = str(shape.get("plan_class", "unknown"))
                shape_counts.update([plan_class])
                total_plans += 1
                matched = bool(shape.get("matches_acceptable_remediation"))
                if matched:
                    matched_plans += 1
                    run_matched = True
                if plan_class == "read_then_mutate":
                    read_then_mutate_plan_count += 1
                param_structure = shape.get("mutating_param_structure", [])
                if isinstance(param_structure, list):
                    mutating_steps_with_read_refs += sum(
                        1
                        for item in param_structure
                        if isinstance(item, dict) and bool(item.get("references_read_outputs"))
                    )
                read_actions.update(shape.get("read_action_keys", []))
                mutating_actions.update(shape.get("mutating_action_keys", []))
            if run_matched:
                any_plan_matched_runs += 1
                any_plan_matched += 1

        scenario_match_breakdown[scenario_id] = {
            "top_ranked_matched_runs": top_ranked_matched,
            "any_plan_matched_runs": any_plan_matched,
            "total_runs": len(runs),
        }
        scenario_action_breakdown[scenario_id] = {
            "read_action_keys": sorted(read_actions),
            "mutating_action_keys": sorted(mutating_actions),
        }

    mean_latency = (sum(latency_values) / len(latency_values)) if latency_values else 0.0
    return {
        "scenario_count": len(scenarios),
        "runs_per_scenario": max((len(item.get("runs", [])) for item in scenarios), default=0),
        "total_plans": total_plans,
        "shape_counts": dict(sorted(shape_counts.items())),
        "first_mutating_match_rate": (matched_plans / total_plans) if total_plans else 0.0,
        "top_ranked_match": {
            "matched_runs": top_ranked_matched_runs,
            "total_runs": total_runs,
            "rate": (top_ranked_matched_runs / total_runs) if total_runs else 0.0,
        },
        "any_plan_match": {
            "matched_runs": any_plan_matched_runs,
            "total_runs": total_runs,
            "rate": (any_plan_matched_runs / total_runs) if total_runs else 0.0,
        },
        "mean_latency_ms": mean_latency,
        "scenario_match_breakdown": scenario_match_breakdown,
        "read_then_mutate_plans": read_then_mutate_plan_count,
        "mutating_steps_with_read_output_refs": mutating_steps_with_read_refs,
        "scenario_action_breakdown": scenario_action_breakdown,
    }


def _snapshot_app_tree(app_root: Path) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    for path in sorted(app_root.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        rel = path.relative_to(app_root).as_posix()
        if path.is_symlink():
            snapshot[rel] = {"kind": "symlink", "target": path.readlink().as_posix()}
            continue
        if path.is_file():
            stat_size = path.stat().st_size
            try:
                digest = sha256(path.read_bytes()).hexdigest()
                snapshot[rel] = {
                    "kind": "file",
                    "size": stat_size,
                    "sha256": digest,
                }
            except PermissionError:
                snapshot[rel] = {
                    "kind": "file",
                    "size": stat_size,
                    "sha256": "unreadable",
                    "read_error": "permission_denied",
                }
            except OSError as exc:
                snapshot[rel] = {
                    "kind": "file",
                    "size": stat_size,
                    "sha256": "unreadable",
                    "read_error": type(exc).__name__,
                }
    return snapshot


def _diff_file_snapshots(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> dict[str, list[str]]:
    added = sorted(path for path in after if path not in before)
    removed = sorted(path for path in before if path not in after)
    changed = sorted(
        path
        for path in (set(before) & set(after))
        if before[path] != after[path]
    )
    return {"added": added, "removed": removed, "changed": changed}


def _capture_db_rowcount_snapshot(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {"exists": False, "tables": {}, "total_rows": 0}

    def _sync_snapshot() -> dict[str, Any]:
        with sqlite3.connect(db_path) as conn:
            table_rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            tables = [str(row[0]) for row in table_rows]
            counts: dict[str, int] = {}
            for table in tables:
                try:
                    count = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                except sqlite3.Error:
                    count = -1
                counts[table] = count
        return {
            "exists": True,
            "tables": dict(sorted(counts.items())),
            "total_rows": int(sum(value for value in counts.values() if value >= 0)),
        }

    try:
        return _sync_snapshot()
    except sqlite3.Error:
        return {"exists": True, "tables": {}, "total_rows": -1, "error": "sqlite_error"}


def _data_preserved(
    before_data: dict[str, Any],
    after_data: dict[str, Any],
) -> bool:
    if not bool(before_data.get("exists", False)):
        return bool(after_data.get("exists", False))
    if not bool(after_data.get("exists", False)):
        return False
    return before_data.get("tables") == after_data.get("tables")


def _related_path_allowlist_for_scenario(scenario_id: str) -> set[str]:
    common = {
        "data/app.db",
        "data/app.db-wal",
        "data/app.db-journal",
    }
    scenario_specific = {
        "S05": {"data/app.db-wal"},
        "S06": {"data/app.db-journal"},
        "S07": {"tmp_volume_fill.bin"},
        "S10": {"config/runtime.env"},
    }
    return common | scenario_specific.get(scenario_id, set())


def _collateral_damage(
    *,
    before_files: dict[str, dict[str, Any]],
    after_files: dict[str, dict[str, Any]],
    scenario_id: str,
) -> dict[str, Any]:
    diff = _diff_file_snapshots(before_files, after_files)
    changed_paths = sorted(diff["added"] + diff["removed"] + diff["changed"])
    allowed = _related_path_allowlist_for_scenario(scenario_id)
    unrelated = [path for path in changed_paths if path not in allowed]
    return {
        "changed_paths": changed_paths,
        "allowed_paths": sorted(allowed),
        "unrelated_changed_paths": unrelated,
        "no_collateral_damage": len(unrelated) == 0,
    }


def _first_executable_action_for_plan_shape(
    *,
    tools: ToolExecutor,
    action_keys: list[str],
) -> str | None:
    for action_id in action_keys:
        if _is_executable_step(tools, action_id):
            return action_id
    return None


def _select_route_parameters(
    *,
    action_id: str,
    previous_results: dict[str, dict[str, Any]],
    app_root: Path,
) -> dict[str, Any]:
    if action_id == "request_backup_restore":
        backups = previous_results.get("list_backups", {}).get("backups", [])
        if isinstance(backups, list) and backups:
            first = backups[0]
            if isinstance(first, dict) and isinstance(first.get("backup_id"), str):
                return {"backup_id": first["backup_id"]}
        return {}
    if action_id == "recover_db_to_new_file":
        return {"source_db_path": "data/app.db", "output_path": "data/recovered.db"}
    if action_id == "move_path":
        return {
            "source_path": "data/recovered.db",
            "destination_path": "data/app.db",
            "overwrite": True,
        }
    if action_id == "remove_path":
        journal_path = app_root / "data" / "app.db-journal"
        if journal_path.exists():
            return {"path": "data/app.db-journal", "recursive": False}
        return {"path": "tmp_volume_fill.bin", "recursive": False}
    if action_id == "set_permissions":
        return {"path": "data/app.db", "mode": 0o660}
    if action_id == "write_app_file":
        return {"path": "config/runtime.env", "content": "DB_PATH=/target/app/data/app.db\n"}
    if action_id == "escalate_to_operator":
        return {
            "summary": "Escalating during route validation",
            "observations": ["route_validation"],
            "actions_taken": [],
            "uncertainty": "Route includes escalation",
        }
    return {}


async def _execute_reference_route(
    *,
    tools: ToolExecutor,
    app_root: Path,
    route: tuple[str, ...],
) -> dict[str, Any]:
    previous_results: dict[str, dict[str, Any]] = {}
    executed: list[dict[str, Any]] = []
    for action_id in route:
        parameters = _select_route_parameters(
            action_id=action_id,
            previous_results=previous_results,
            app_root=app_root,
        )
        try:
            validated = tools.with_default_parameters(action_id, parameters)
            result = await tools.execute(
                action_id,
                validated,
                allowed_primitives=tools.primitive_ids,
            )
            previous_results[action_id] = result.data
            executed.append(
                {
                    "action_id": action_id,
                    "parameters": dict(sorted(validated.items())),
                    "status": "executed",
                }
            )
        except Exception as exc:
            executed.append(
                {
                    "action_id": action_id,
                    "parameters": dict(sorted(parameters.items())),
                    "ok": False,
                    "error": str(exc),
                }
            )
            return {
                "route": list(route),
                "executed": executed,
                "success": False,
                "error": str(exc),
            }
    return {"route": list(route), "executed": executed, "success": True}


async def _run_planner_shapes(args: argparse.Namespace) -> dict[str, Any]:
    scenario_ids = _parse_scenario_ids(args.scenarios)

    base_settings = MISettings()
    baseline_app_root = (args.baseline_app_root or base_settings.app_root).resolve()
    if not baseline_app_root.exists() or not baseline_app_root.is_dir():
        raise SystemExit(f"Baseline app root does not exist: {baseline_app_root}")

    acceptable_overrides = _load_acceptable_remediations(args.acceptable_remediations)

    llm_settings = {
        "llm_endpoint": base_settings.llm_endpoint,
        "llm_model": base_settings.llm_model,
        "llm_small_model": base_settings.llm_small_model,
        "llm_large_model": base_settings.llm_large_model,
    }

    scenario_reports: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="mi-real-planner-") as temp_dir:
        temp_root = Path(temp_dir)

        for index, scenario_id in enumerate(scenario_ids):
            sandbox_root = temp_root / scenario_id
            app_root = sandbox_root / "app"
            backups_root = sandbox_root / "backups"
            state_dir = sandbox_root / "state"
            copytree(baseline_app_root, app_root)
            backups_root.mkdir(parents=True, exist_ok=True)
            state_dir.mkdir(parents=True, exist_ok=True)

            settings = base_settings.model_copy(
                update={
                    "sandbox_root": sandbox_root,
                    "app_root": app_root,
                    "backups_root": backups_root,
                    "db_file": app_root / "data" / "app.db",
                    "app_log": app_root / "app.log",
                    "schema_file": app_root / "schema.sql",
                    "state_dir": state_dir,
                    "policy_store_path": state_dir / "policy_store.json",
                    "skill_store_path": state_dir / "skills.md",
                    "decision_log_path": state_dir / "decision_log.jsonl",
                    "event_log_path": state_dir / "events.jsonl",
                    "failure_taxonomy_path": state_dir / "failure_taxonomy.json",
                }
            )

            event_bus = EventBus(settings.event_log_path)
            tools = ToolExecutor(settings, event_bus)
            policy = PolicyStore(
                settings.policy_store_path,
                recency_alpha=settings.policy_recency_alpha,
            )
            llm = OpenAILLMClient(settings)
            diagnoser = Diagnoser(llm, tools, policy, settings)

            seed = args.base_seed + index
            injection = run_injector(
                scenario_id,
                InjectorContext(
                    sandbox_root=sandbox_root,
                    app_root=app_root,
                    db_file=settings.db_file,
                    backups_root=backups_root,
                ),
                seed=seed,
            )

            logs = await diagnoser.collect_logs()
            diagnosis, _, fingerprint = await diagnoser.diagnose(
                failure_type_hint=None,
                recent_logs=logs,
            )

            acceptable_actions = _resolve_acceptable_actions(
                tools=tools,
                scenario_id=scenario_id,
                overrides=acceptable_overrides,
            )
            run_reports: list[dict[str, Any]] = []
            for run_index in range(1, args.runs + 1):
                usage_before = llm.usage.estimated_cost_usd
                started_at = perf_counter()
                plans = await llm.propose_plans(
                    diagnosis=diagnosis,
                    available_actions=sorted(tools.primitive_ids),
                    action_parameter_contracts=tools.primitive_parameter_contracts(),
                    recalled_skills=[],
                    failed_action_keys=[],
                    constraints={
                        "state_snapshot_hash": fingerprint["probes"],
                        "run_index": run_index,
                    },
                )
                latency_ms = max(0.0, (perf_counter() - started_at) * 1000.0)
                usage_after = llm.usage.estimated_cost_usd
                token_estimate = int(max(0.0, usage_after - usage_before) / 0.000001)

                shapes = [
                    _classify_plan_shape(
                        tools=tools,
                        plan=plan,
                        acceptable_actions=acceptable_actions,
                    )
                    for plan in plans
                ]
                run_reports.append(
                    {
                        "run_index": run_index,
                        "latency_ms": latency_ms,
                        "token_estimate": token_estimate,
                        "plan_count": len(shapes),
                        "run_matched": any(
                            shape.matches_acceptable_remediation for shape in shapes
                        ),
                        "plan_shapes": [_serialize_shape(shape) for shape in shapes],
                    }
                )

            scenario_reports.append(
                {
                    "scenario_id": scenario_id,
                    "seed": seed,
                    "injected_fault": SCENARIO_BY_ID[scenario_id].injected_fault,
                    "notes": injection.notes,
                    "acceptable_first_mutating_actions": sorted(acceptable_actions),
                    "runs": run_reports,
                }
            )

    report = {
        "mode": "real_planner_shapes",
        "llm": llm_settings,
        "baseline_app_root": str(baseline_app_root),
        "scenarios": scenario_reports,
    }
    report["aggregate"] = _summarize_planner_shapes(report)
    return report


def _rescore_existing_report(args: argparse.Namespace) -> dict[str, Any]:
    if args.input_report is None:
        raise SystemExit("--mode rescore requires --input-report")

    report = json.loads(args.input_report.read_text(encoding="utf-8"))
    scenarios = report.get("scenarios")
    if not isinstance(scenarios, list):
        raise SystemExit("input report must contain a scenarios array")

    base_settings = MISettings()
    event_bus = EventBus()
    tools = ToolExecutor(base_settings, event_bus)
    acceptable_overrides = _load_acceptable_remediations(args.acceptable_remediations)

    for scenario_report in scenarios:
        scenario_id = str(scenario_report.get("scenario_id", "")).strip()
        if scenario_id not in SCENARIO_BY_ID:
            continue
        acceptable_actions = _resolve_acceptable_actions(
            tools=tools,
            scenario_id=scenario_id,
            overrides=acceptable_overrides,
        )
        scenario_report["acceptable_first_mutating_actions"] = sorted(acceptable_actions)
        scenario_report["injected_fault"] = SCENARIO_BY_ID[scenario_id].injected_fault

        for run in scenario_report.get("runs", []):
            plan_shapes = run.get("plan_shapes", [])
            for shape in plan_shapes:
                _enrich_shape_from_action_ids(shape, tools)
                first_action = shape.get("first_executable_action")
                matched = (
                    first_action in acceptable_actions
                    if isinstance(first_action, str)
                    else False
                )
                shape["matches_acceptable_remediation"] = matched
                shape["matches_expected_remediation"] = matched
            run["run_matched"] = any(
                bool(item.get("matches_acceptable_remediation")) for item in plan_shapes
            )

    report["mode"] = "real_planner_shapes_rescored"
    report["aggregate"] = _summarize_planner_shapes(report)
    return report


def _parse_action_id_from_action_key(action_key: str) -> str:
    raw = action_key.split("|", 1)[0]
    if raw.startswith("skill:"):
        return raw
    return raw


async def _run_full_loop_compare(args: argparse.Namespace) -> dict[str, Any]:
    scenario_ids = _parse_scenario_ids(args.scenarios)
    if args.runs < 1:
        raise SystemExit("full_loop_compare requires --runs >= 1")

    base_settings = MISettings()
    baseline_app_root = (args.baseline_app_root or base_settings.app_root).resolve()
    if not baseline_app_root.exists() or not baseline_app_root.is_dir():
        raise SystemExit(f"Baseline app root does not exist: {baseline_app_root}")

    acceptable_overrides = _load_acceptable_remediations(args.acceptable_remediations)
    comparison_reports: list[dict[str, Any]] = []

    for readonly_enabled in (False, True):
        scenario_reports: list[dict[str, Any]] = []
        mode_label = "on" if readonly_enabled else "off"
        with tempfile.TemporaryDirectory(prefix=f"mi-full-loop-{mode_label}-") as temp_dir:
            temp_root = Path(temp_dir)
            for scenario_index, scenario_id in enumerate(scenario_ids):
                scenario = SCENARIO_BY_ID[scenario_id]
                run_reports: list[dict[str, Any]] = []

                for run_index in range(1, args.runs + 1):
                    sandbox_root = temp_root / scenario_id / f"run_{run_index}"
                    app_root = sandbox_root / "app"
                    backups_root = sandbox_root / "backups"
                    state_dir = sandbox_root / "state"
                    copytree(baseline_app_root, app_root)
                    backups_root.mkdir(parents=True, exist_ok=True)
                    state_dir.mkdir(parents=True, exist_ok=True)

                    settings = base_settings.model_copy(
                        update={
                            "readonly_replan_enabled": readonly_enabled,
                            "sandbox_root": sandbox_root,
                            "app_root": app_root,
                            "backups_root": backups_root,
                            "db_file": app_root / "data" / "app.db",
                            "app_log": app_root / "app.log",
                            "schema_file": app_root / "schema.sql",
                            "state_dir": state_dir,
                            "policy_store_path": state_dir / "policy_store.json",
                            "skill_store_path": state_dir / "skills.md",
                            "decision_log_path": state_dir / "decision_log.jsonl",
                            "event_log_path": state_dir / "events.jsonl",
                            "failure_taxonomy_path": state_dir / "failure_taxonomy.json",
                        }
                    )
                    seed = args.base_seed + (scenario_index * args.runs) + run_index

                    recording_llm = RecordingLLMClient(OpenAILLMClient(settings))
                    agent, context = build_agent(settings, recording_llm)

                    async def _health_true() -> bool:
                        return True

                    context.verifier.health_endpoint_ok = _health_true  # type: ignore[method-assign]

                    run_injector(
                        scenario_id,
                        InjectorContext(
                            sandbox_root=sandbox_root,
                            app_root=app_root,
                            db_file=settings.db_file,
                            backups_root=backups_root,
                        ),
                        seed=seed,
                    )

                    started_at = perf_counter()
                    await agent._recover_loop()
                    wall_time_ms = max(0.0, (perf_counter() - started_at) * 1000.0)

                    tools = context.tools
                    acceptable_actions = _resolve_acceptable_actions(
                        tools=tools,
                        scenario_id=scenario_id,
                        overrides=acceptable_overrides,
                    )
                    events = list(context.events.events)
                    planner_calls = [
                        event for event in events if event.event_type == "agent.planner_call"
                    ]
                    if not planner_calls:
                        llm_events = [
                            event
                            for event in events
                            if event.event_type.startswith("llm.")
                            or event.event_type == "agent.degraded_mode"
                        ]
                        llm_details = [
                            {
                                "event_type": event.event_type,
                                "payload": event.payload,
                            }
                            for event in llm_events[-5:]
                        ]
                        raise RuntimeError(
                            "full_loop_compare aborted: planner unreachable or bypassed "
                            "("
                            f"scenario={scenario_id}, "
                            f"run={run_index}, "
                            f"readonly={readonly_enabled}"
                            "). "
                            f"recent_events={json.dumps(llm_details, sort_keys=True)}"
                        )
                    readonly_rounds = [
                        event
                        for event in events
                        if event.event_type == "agent.readonly_diagnosis_round"
                    ]
                    verifier_events = [
                        event for event in events if event.event_type == "verifier.result"
                    ]
                    action_events = [
                        event for event in events if event.event_type == "agent.action_executed"
                    ]

                    executed_action_ids = [
                        _parse_action_id_from_action_key(
                            str(event.payload.get("action_key", ""))
                        )
                        for event in action_events
                    ]
                    first_executable_action: str | None = None
                    for action_id in executed_action_ids:
                        if action_id.startswith("skill:"):
                            continue
                        if _is_executable_step(tools, action_id):
                            first_executable_action = action_id
                            break

                    remediation_in_acceptable_set = (
                        first_executable_action in acceptable_actions
                        if first_executable_action is not None
                        else False
                    )
                    recovery_verified = any(
                        bool(event.payload.get("healthy", False)) for event in verifier_events
                    )

                    planner_shapes: list[dict[str, Any]] = []
                    for call_index, plans in enumerate(recording_llm.plan_calls, start=1):
                        shapes = [
                            _classify_plan_shape(
                                tools=tools,
                                plan=plan,
                                acceptable_actions=acceptable_actions,
                            )
                            for plan in plans
                        ]
                        planner_shapes.append(
                            {
                                "planner_call": call_index,
                                "plan_count": len(shapes),
                                "plans": [_serialize_shape(shape) for shape in shapes],
                            }
                        )

                    run_reports.append(
                        {
                            "run_index": run_index,
                            "seed": seed,
                            "rounds_used": len(readonly_rounds),
                            "planner_calls": len(planner_calls),
                            "wall_time_ms": wall_time_ms,
                            "executed_action_ids": executed_action_ids,
                            "first_executable_action": first_executable_action,
                            "remediation_in_acceptable_set": remediation_in_acceptable_set,
                            "recovery_verified": recovery_verified,
                            "acceptable_first_mutating_actions": sorted(acceptable_actions),
                            "planner_shapes": planner_shapes,
                        }
                    )

                scenario_reports.append(
                    {
                        "scenario_id": scenario_id,
                        "injected_fault": scenario.injected_fault,
                        "runs": run_reports,
                    }
                )

        aggregate = {
            "scenario_count": len(scenario_reports),
            "runs_per_scenario": args.runs,
            "total_runs": len(scenario_reports) * args.runs,
            "recovery_verified_rate": 0.0,
            "acceptable_remediation_rate": 0.0,
            "avg_rounds_used": 0.0,
            "avg_planner_calls": 0.0,
            "avg_wall_time_ms": 0.0,
            "scenario_breakdown": {},
        }
        total_runs = 0
        recovered_runs = 0
        acceptable_runs = 0
        rounds_sum = 0
        planner_calls_sum = 0
        wall_sum = 0.0
        for scenario_report in scenario_reports:
            matched = 0
            recovered = 0
            rounds_total = 0
            planner_total = 0
            wall_total = 0.0
            for run in scenario_report["runs"]:
                total_runs += 1
                rounds_sum += run["rounds_used"]
                planner_calls_sum += run["planner_calls"]
                wall_sum += run["wall_time_ms"]
                rounds_total += run["rounds_used"]
                planner_total += run["planner_calls"]
                wall_total += run["wall_time_ms"]
                if run["remediation_in_acceptable_set"]:
                    acceptable_runs += 1
                    matched += 1
                if run["recovery_verified"]:
                    recovered_runs += 1
                    recovered += 1
            aggregate["scenario_breakdown"][scenario_report["scenario_id"]] = {
                "matched_runs": matched,
                "recovered_runs": recovered,
                "total_runs": len(scenario_report["runs"]),
                "avg_rounds_used": (
                    rounds_total / len(scenario_report["runs"]) if scenario_report["runs"] else 0.0
                ),
                "avg_planner_calls": (
                    planner_total / len(scenario_report["runs"]) if scenario_report["runs"] else 0.0
                ),
                "avg_wall_time_ms": (
                    wall_total / len(scenario_report["runs"]) if scenario_report["runs"] else 0.0
                ),
            }

        if total_runs > 0:
            aggregate["recovery_verified_rate"] = recovered_runs / total_runs
            aggregate["acceptable_remediation_rate"] = acceptable_runs / total_runs
            aggregate["avg_rounds_used"] = rounds_sum / total_runs
            aggregate["avg_planner_calls"] = planner_calls_sum / total_runs
            aggregate["avg_wall_time_ms"] = wall_sum / total_runs

        comparison_reports.append(
            {
                "readonly_replan_enabled": readonly_enabled,
                "scenarios": scenario_reports,
                "aggregate": aggregate,
            }
        )

    return {
        "mode": "full_loop_compare",
        "baseline_app_root": str(baseline_app_root),
        "comparison": comparison_reports,
    }


def _analyze_top_rank(args: argparse.Namespace) -> dict[str, Any]:
    if args.input_report is None:
        raise SystemExit("--mode analyze_top_rank requires --input-report")

    report = json.loads(args.input_report.read_text(encoding="utf-8"))
    scenarios = report.get("scenarios")
    if not isinstance(scenarios, list):
        raise SystemExit("input report must contain a scenarios array")

    base_settings = MISettings()
    tools = ToolExecutor(base_settings, EventBus())

    runs: list[dict[str, Any]] = []
    top_ranked_match_count = 0
    any_plan_match_count = 0
    rank_distribution: Counter[int] = Counter()

    for scenario in scenarios:
        scenario_id = str(scenario.get("scenario_id", "")).strip()
        acceptable_actions = set(scenario.get("acceptable_first_mutating_actions") or [])
        for run in scenario.get("runs", []):
            run_index = int(run.get("run_index", 0))
            plan_shapes = run.get("plan_shapes", [])
            plan_order: list[dict[str, Any]] = []
            first_acceptable_rank: int | None = None
            for rank, shape in enumerate(plan_shapes, start=1):
                action_keys = shape.get("action_keys") or []
                if not isinstance(action_keys, list) or not all(
                    isinstance(item, str) for item in action_keys
                ):
                    action_keys = []
                first_executable_action = shape.get("first_executable_action")
                if not isinstance(first_executable_action, str):
                    first_executable_action = _first_executable_action_for_plan_shape(
                        tools=tools,
                        action_keys=action_keys,
                    )
                matched = (
                    first_executable_action in acceptable_actions
                    if first_executable_action is not None
                    else False
                )
                if matched and first_acceptable_rank is None:
                    first_acceptable_rank = rank
                plan_order.append(
                    {
                        "rank": rank,
                        "plan_name": shape.get("plan_name"),
                        "first_executable_action": first_executable_action,
                        "action_keys": action_keys,
                        "matches_acceptable": matched,
                    }
                )
            top_ranked_match = bool(plan_order and plan_order[0]["matches_acceptable"])
            any_plan_match = first_acceptable_rank is not None
            if top_ranked_match:
                top_ranked_match_count += 1
            if any_plan_match:
                any_plan_match_count += 1
                rank_distribution[first_acceptable_rank or -1] += 1
            runs.append(
                {
                    "scenario_id": scenario_id,
                    "run_index": run_index,
                    "acceptable_first_mutating_actions": sorted(acceptable_actions),
                    "plan_order": plan_order,
                    "first_acceptable_rank": first_acceptable_rank,
                    "top_ranked_match": top_ranked_match,
                    "any_plan_match": any_plan_match,
                }
            )

    total_runs = len(runs)
    return {
        "mode": "analyze_top_rank",
        "input_report": str(args.input_report),
        "summary": {
            "total_runs": total_runs,
            "top_ranked_match": {
                "matched_runs": top_ranked_match_count,
                "total_runs": total_runs,
                "rate": (top_ranked_match_count / total_runs) if total_runs else 0.0,
            },
            "any_plan_match": {
                "matched_runs": any_plan_match_count,
                "total_runs": total_runs,
                "rate": (any_plan_match_count / total_runs) if total_runs else 0.0,
            },
            "first_acceptable_rank_distribution": {
                str(key): value for key, value in sorted(rank_distribution.items())
            },
        },
        "runs": runs,
    }


async def _run_full_loop_outcome_compare(args: argparse.Namespace) -> dict[str, Any]:
    scenario_ids = _parse_scenario_ids(args.scenarios)
    if args.runs < 1:
        raise SystemExit("full_loop_outcome_compare requires --runs >= 1")

    base_settings = MISettings()
    baseline_app_root = (args.baseline_app_root or base_settings.app_root).resolve()
    if not baseline_app_root.exists() or not baseline_app_root.is_dir():
        raise SystemExit(f"Baseline app root does not exist: {baseline_app_root}")

    acceptable_overrides = _load_acceptable_remediations(args.acceptable_remediations)
    comparison_reports: list[dict[str, Any]] = []

    for readonly_enabled in (False, True):
        scenario_reports: list[dict[str, Any]] = []
        mode_label = "on" if readonly_enabled else "off"
        with tempfile.TemporaryDirectory(prefix=f"mi-outcome-compare-{mode_label}-") as temp_dir:
            temp_root = Path(temp_dir)
            for scenario_index, scenario_id in enumerate(scenario_ids):
                scenario = SCENARIO_BY_ID[scenario_id]
                run_reports: list[dict[str, Any]] = []

                for run_index in range(1, args.runs + 1):
                    sandbox_root = temp_root / scenario_id / f"run_{run_index}"
                    app_root = sandbox_root / "app"
                    backups_root = sandbox_root / "backups"
                    state_dir = sandbox_root / "state"
                    copytree(baseline_app_root, app_root)
                    backups_root.mkdir(parents=True, exist_ok=True)
                    state_dir.mkdir(parents=True, exist_ok=True)

                    before_files = _snapshot_app_tree(app_root)
                    before_data = _capture_db_rowcount_snapshot(app_root / "data" / "app.db")

                    settings = base_settings.model_copy(
                        update={
                            "readonly_replan_enabled": readonly_enabled,
                            "sandbox_root": sandbox_root,
                            "app_root": app_root,
                            "backups_root": backups_root,
                            "db_file": app_root / "data" / "app.db",
                            "app_log": app_root / "app.log",
                            "schema_file": app_root / "schema.sql",
                            "state_dir": state_dir,
                            "policy_store_path": state_dir / "policy_store.json",
                            "skill_store_path": state_dir / "skills.md",
                            "decision_log_path": state_dir / "decision_log.jsonl",
                            "event_log_path": state_dir / "events.jsonl",
                            "failure_taxonomy_path": state_dir / "failure_taxonomy.json",
                        }
                    )
                    seed = args.base_seed + (scenario_index * args.runs) + run_index

                    recording_llm = RecordingLLMClient(OpenAILLMClient(settings))
                    agent, context = build_agent(settings, recording_llm)

                    async def _health_true() -> bool:
                        return True

                    context.verifier.health_endpoint_ok = _health_true  # type: ignore[method-assign]

                    run_injector(
                        scenario_id,
                        InjectorContext(
                            sandbox_root=sandbox_root,
                            app_root=app_root,
                            db_file=settings.db_file,
                            backups_root=backups_root,
                        ),
                        seed=seed,
                    )

                    started_at = perf_counter()
                    await agent._recover_loop()
                    wall_time_ms = max(0.0, (perf_counter() - started_at) * 1000.0)

                    tools = context.tools
                    acceptable_actions = _resolve_acceptable_actions(
                        tools=tools,
                        scenario_id=scenario_id,
                        overrides=acceptable_overrides,
                    )
                    events = list(context.events.events)
                    planner_calls = [
                        event for event in events if event.event_type == "agent.planner_call"
                    ]
                    if not planner_calls:
                        llm_events = [
                            event
                            for event in events
                            if event.event_type.startswith("llm.")
                            or event.event_type == "agent.degraded_mode"
                        ]
                        llm_details = [
                            {
                                "event_type": event.event_type,
                                "payload": event.payload,
                            }
                            for event in llm_events[-5:]
                        ]
                        raise RuntimeError(
                            "full_loop_outcome_compare aborted: planner unreachable or bypassed "
                            "("
                            f"scenario={scenario_id}, "
                            f"run={run_index}, "
                            f"readonly={readonly_enabled}"
                            "). "
                            f"recent_events={json.dumps(llm_details, sort_keys=True)}"
                        )

                    readonly_rounds = [
                        event
                        for event in events
                        if event.event_type == "agent.readonly_diagnosis_round"
                    ]
                    verifier_events = [
                        event for event in events if event.event_type == "verifier.result"
                    ]
                    action_events = [
                        event for event in events if event.event_type == "agent.action_executed"
                    ]

                    executed_action_ids = [
                        _parse_action_id_from_action_key(
                            str(event.payload.get("action_key", ""))
                        )
                        for event in action_events
                    ]
                    first_executable_action: str | None = None
                    for action_id in executed_action_ids:
                        if action_id.startswith("skill:"):
                            continue
                        if _is_executable_step(tools, action_id):
                            first_executable_action = action_id
                            break

                    remediation_in_acceptable_set = (
                        first_executable_action in acceptable_actions
                        if first_executable_action is not None
                        else False
                    )
                    planner_candidate_first_executable_actions_by_round: list[dict[str, Any]] = []
                    planner_candidate_first_executable_actions: list[str] = []
                    for planner_call_index, plans in enumerate(recording_llm.plan_calls, start=1):
                        round_actions: list[str] = []
                        for plan in plans:
                            shape = _classify_plan_shape(
                                tools=tools,
                                plan=plan,
                                acceptable_actions=acceptable_actions,
                            )
                            if shape.first_executable_action is not None:
                                round_actions.append(shape.first_executable_action)
                                planner_candidate_first_executable_actions.append(
                                    shape.first_executable_action
                                )

                        planner_candidate_first_executable_actions_by_round.append(
                            {
                                "planner_call": planner_call_index,
                                "first_executable_actions": round_actions,
                                "has_acceptable_candidate": any(
                                    action in acceptable_actions for action in round_actions
                                ),
                            }
                        )

                    correct_action_in_candidates = any(
                        action in acceptable_actions
                        for action in planner_candidate_first_executable_actions
                    )
                    action_name_match_non_escalation = (
                        remediation_in_acceptable_set
                        and first_executable_action != "escalate_to_operator"
                    )
                    escalation_action = "escalate_to_operator" in executed_action_ids
                    recovery_verified = any(
                        bool(event.payload.get("healthy", False)) for event in verifier_events
                    )
                    after_data = _capture_db_rowcount_snapshot(settings.db_file)
                    data_preserved = _data_preserved(before_data, after_data)
                    after_files = _snapshot_app_tree(app_root)
                    collateral = _collateral_damage(
                        before_files=before_files,
                        after_files=after_files,
                        scenario_id=scenario_id,
                    )
                    no_collateral_damage = bool(collateral["no_collateral_damage"])
                    strict_success = (
                        recovery_verified
                        and data_preserved
                        and no_collateral_damage
                        and not escalation_action
                    )

                    run_reports.append(
                        {
                            "run_index": run_index,
                            "seed": seed,
                            "rounds_used": len(readonly_rounds),
                            "planner_calls": len(planner_calls),
                            "wall_time_ms": wall_time_ms,
                            "executed_action_ids": executed_action_ids,
                            "first_executable_action": first_executable_action,
                            "acceptable_first_mutating_actions": sorted(acceptable_actions),
                            "planner_candidate_first_executable_actions_by_round": (
                                planner_candidate_first_executable_actions_by_round
                            ),
                            "planner_candidate_first_executable_actions": (
                                planner_candidate_first_executable_actions
                            ),
                            "correct_action_in_candidates": correct_action_in_candidates,
                            "action_name_match": remediation_in_acceptable_set,
                            "action_name_match_non_escalation": action_name_match_non_escalation,
                            "escalation_action": escalation_action,
                            "recovery_verified": recovery_verified,
                            "data_preserved": data_preserved,
                            "data_before": before_data,
                            "data_after": after_data,
                            "no_collateral_damage": no_collateral_damage,
                            "collateral": collateral,
                            "strict_success": strict_success,
                        }
                    )

                scenario_reports.append(
                    {
                        "scenario_id": scenario_id,
                        "injected_fault": scenario.injected_fault,
                        "runs": run_reports,
                    }
                )

        aggregate = {
            "scenario_count": len(scenario_reports),
            "runs_per_scenario": args.runs,
            "total_runs": len(scenario_reports) * args.runs,
            "strict_success_rate": 0.0,
            "recovery_verified_rate": 0.0,
            "data_preserved_rate": 0.0,
            "no_collateral_damage_rate": 0.0,
            "escalation_rate": 0.0,
            "action_name_match_rate": 0.0,
            "action_name_match_non_escalation_rate": 0.0,
            "correct_action_in_candidates_rate": 0.0,
            "strict_success_given_correct_candidates_rate": 0.0,
            "correct_action_in_candidates_runs": 0,
            "strict_success_given_correct_candidates_runs": 0,
            "strict_success_given_correct_candidates_denominator": 0,
            "avg_rounds_used": 0.0,
            "avg_planner_calls": 0.0,
            "avg_wall_time_ms": 0.0,
            "scenario_breakdown": {},
        }

        total_runs = 0
        strict_success_count = 0
        recovery_count = 0
        preserved_count = 0
        no_collateral_count = 0
        escalation_count = 0
        action_match_count = 0
        action_match_non_escalation_count = 0
        correct_action_in_candidates_count = 0
        strict_given_correct_candidates_count = 0
        rounds_sum = 0
        planner_calls_sum = 0
        wall_sum = 0.0

        for scenario_report in scenario_reports:
            scenario_strict = 0
            scenario_recovery = 0
            scenario_preserved = 0
            scenario_no_collateral = 0
            scenario_escalation = 0
            scenario_action_match = 0
            scenario_action_non_escalation = 0
            scenario_correct_candidates = 0
            scenario_strict_given_correct_candidates = 0
            scenario_rounds_sum = 0
            scenario_planner_sum = 0
            scenario_wall_sum = 0.0

            for run in scenario_report["runs"]:
                total_runs += 1
                rounds_sum += run["rounds_used"]
                planner_calls_sum += run["planner_calls"]
                wall_sum += run["wall_time_ms"]
                scenario_rounds_sum += run["rounds_used"]
                scenario_planner_sum += run["planner_calls"]
                scenario_wall_sum += run["wall_time_ms"]

                if run["strict_success"]:
                    strict_success_count += 1
                    scenario_strict += 1
                if run["recovery_verified"]:
                    recovery_count += 1
                    scenario_recovery += 1
                if run["data_preserved"]:
                    preserved_count += 1
                    scenario_preserved += 1
                if run["no_collateral_damage"]:
                    no_collateral_count += 1
                    scenario_no_collateral += 1
                if run["escalation_action"]:
                    escalation_count += 1
                    scenario_escalation += 1
                if run["action_name_match"]:
                    action_match_count += 1
                    scenario_action_match += 1
                if run["action_name_match_non_escalation"]:
                    action_match_non_escalation_count += 1
                    scenario_action_non_escalation += 1
                if run["correct_action_in_candidates"]:
                    correct_action_in_candidates_count += 1
                    scenario_correct_candidates += 1
                    if run["strict_success"]:
                        strict_given_correct_candidates_count += 1
                        scenario_strict_given_correct_candidates += 1

            scenario_total = len(scenario_report["runs"])
            aggregate["scenario_breakdown"][scenario_report["scenario_id"]] = {
                "strict_success_runs": scenario_strict,
                "recovery_verified_runs": scenario_recovery,
                "data_preserved_runs": scenario_preserved,
                "no_collateral_damage_runs": scenario_no_collateral,
                "escalation_runs": scenario_escalation,
                "action_name_match_runs": scenario_action_match,
                "action_name_match_non_escalation_runs": scenario_action_non_escalation,
                "correct_action_in_candidates_runs": scenario_correct_candidates,
                "strict_success_given_correct_candidates_runs": (
                    scenario_strict_given_correct_candidates
                ),
                "strict_success_given_correct_candidates_denominator": scenario_correct_candidates,
                "total_runs": scenario_total,
                "correct_action_in_candidates_rate": (
                    scenario_correct_candidates / scenario_total if scenario_total else 0.0
                ),
                "strict_success_given_correct_candidates_rate": (
                    scenario_strict_given_correct_candidates / scenario_correct_candidates
                    if scenario_correct_candidates
                    else 0.0
                ),
                "avg_rounds_used": (
                    scenario_rounds_sum / scenario_total if scenario_total else 0.0
                ),
                "avg_planner_calls": (
                    scenario_planner_sum / scenario_total if scenario_total else 0.0
                ),
                "avg_wall_time_ms": (scenario_wall_sum / scenario_total) if scenario_total else 0.0,
            }

        if total_runs > 0:
            aggregate["strict_success_rate"] = strict_success_count / total_runs
            aggregate["recovery_verified_rate"] = recovery_count / total_runs
            aggregate["data_preserved_rate"] = preserved_count / total_runs
            aggregate["no_collateral_damage_rate"] = no_collateral_count / total_runs
            aggregate["escalation_rate"] = escalation_count / total_runs
            aggregate["action_name_match_rate"] = action_match_count / total_runs
            aggregate["action_name_match_non_escalation_rate"] = (
                action_match_non_escalation_count / total_runs
            )
            aggregate["correct_action_in_candidates_rate"] = (
                correct_action_in_candidates_count / total_runs
            )
            aggregate["strict_success_given_correct_candidates_rate"] = (
                strict_given_correct_candidates_count / correct_action_in_candidates_count
                if correct_action_in_candidates_count
                else 0.0
            )
            aggregate["correct_action_in_candidates_runs"] = correct_action_in_candidates_count
            aggregate["strict_success_given_correct_candidates_runs"] = (
                strict_given_correct_candidates_count
            )
            aggregate["strict_success_given_correct_candidates_denominator"] = (
                correct_action_in_candidates_count
            )
            aggregate["avg_rounds_used"] = rounds_sum / total_runs
            aggregate["avg_planner_calls"] = planner_calls_sum / total_runs
            aggregate["avg_wall_time_ms"] = wall_sum / total_runs

        comparison_reports.append(
            {
                "readonly_replan_enabled": readonly_enabled,
                "scenarios": scenario_reports,
                "aggregate": aggregate,
            }
        )

    return {
        "mode": "full_loop_outcome_compare",
        "baseline_app_root": str(baseline_app_root),
        "comparison": comparison_reports,
    }


async def _run_validate_reference_routes(args: argparse.Namespace) -> dict[str, Any]:
    scenario_ids = _parse_scenario_ids(args.scenarios)
    base_settings = MISettings()
    baseline_app_root = (args.baseline_app_root or base_settings.app_root).resolve()
    if not baseline_app_root.exists() or not baseline_app_root.is_dir():
        raise SystemExit(f"Baseline app root does not exist: {baseline_app_root}")

    scenario_reports: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="mi-reference-routes-") as temp_dir:
        temp_root = Path(temp_dir)
        for index, scenario_id in enumerate(scenario_ids):
            scenario = SCENARIO_BY_ID[scenario_id]
            route_reports: list[dict[str, Any]] = []

            for route_index, route in enumerate(scenario.reference_routes, start=1):
                sandbox_root = temp_root / scenario_id / f"route_{route_index}"
                app_root = sandbox_root / "app"
                backups_root = sandbox_root / "backups"
                state_dir = sandbox_root / "state"
                copytree(baseline_app_root, app_root)
                backups_root.mkdir(parents=True, exist_ok=True)
                state_dir.mkdir(parents=True, exist_ok=True)

                seed_backup = app_root / "data" / "app.db"
                if seed_backup.exists():
                    (backups_root / "seed_backup.db").write_bytes(seed_backup.read_bytes())

                settings = base_settings.model_copy(
                    update={
                        "sandbox_root": sandbox_root,
                        "app_root": app_root,
                        "backups_root": backups_root,
                        "db_file": app_root / "data" / "app.db",
                        "app_log": app_root / "app.log",
                        "schema_file": app_root / "schema.sql",
                        "state_dir": state_dir,
                        "policy_store_path": state_dir / "policy_store.json",
                        "skill_store_path": state_dir / "skills.md",
                        "decision_log_path": state_dir / "decision_log.jsonl",
                        "event_log_path": state_dir / "events.jsonl",
                        "failure_taxonomy_path": state_dir / "failure_taxonomy.json",
                    }
                )

                _, context = build_agent(settings, OpenAILLMClient(settings))
                tools = context.tools
                verifier = context.verifier

                async def _health_true() -> bool:
                    return True

                verifier.health_endpoint_ok = _health_true  # type: ignore[method-assign]

                before_data = _capture_db_rowcount_snapshot(settings.db_file)

                run_injector(
                    scenario_id,
                    InjectorContext(
                        sandbox_root=sandbox_root,
                        app_root=app_root,
                        db_file=settings.db_file,
                        backups_root=backups_root,
                    ),
                    seed=args.base_seed + index,
                )

                route_execution = await _execute_reference_route(
                    tools=tools,
                    app_root=app_root,
                    route=route,
                )

                verification = await verifier.verify()
                after_data = _capture_db_rowcount_snapshot(settings.db_file)
                data_preserved = _data_preserved(before_data, after_data)
                route_reports.append(
                    {
                        "route": list(route),
                        "route_success": bool(route_execution.get("success", False)),
                        "execution": route_execution,
                        "recovery_verified": verification.healthy,
                        "data_preserved": data_preserved,
                        "data_before": before_data,
                        "data_after": after_data,
                        "route_pass": bool(route_execution.get("success", False))
                        and verification.healthy
                        and data_preserved,
                    }
                )

            scenario_reports.append(
                {
                    "scenario_id": scenario_id,
                    "injected_fault": scenario.injected_fault,
                    "reference_routes": route_reports,
                    "any_route_passed": any(
                        item.get("route_pass", False) for item in route_reports
                    ),
                }
            )

        s01_root = temp_root / "S01_apply_schema_check"
        s01_app = s01_root / "app"
        s01_backups = s01_root / "backups"
        s01_state = s01_root / "state"
        copytree(baseline_app_root, s01_app)
        s01_backups.mkdir(parents=True, exist_ok=True)
        s01_state.mkdir(parents=True, exist_ok=True)
        s01_settings = base_settings.model_copy(
            update={
                "sandbox_root": s01_root,
                "app_root": s01_app,
                "backups_root": s01_backups,
                "db_file": s01_app / "data" / "app.db",
                "app_log": s01_app / "app.log",
                "schema_file": s01_app / "schema.sql",
                "state_dir": s01_state,
                "policy_store_path": s01_state / "policy_store.json",
                "skill_store_path": s01_state / "skills.md",
                "decision_log_path": s01_state / "decision_log.jsonl",
                "event_log_path": s01_state / "events.jsonl",
                "failure_taxonomy_path": s01_state / "failure_taxonomy.json",
            }
        )
        _, s01_context = build_agent(s01_settings, OpenAILLMClient(s01_settings))
        s01_tools = s01_context.tools
        s01_verifier = s01_context.verifier
        s01_verifier.health_endpoint_ok = lambda: asyncio.sleep(0, result=True)  # type: ignore[method-assign]
        if s01_settings.db_file.exists():
            with sqlite3.connect(s01_settings.db_file) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS widgets "
                    "(id INTEGER PRIMARY KEY, name TEXT)"
                )
                conn.execute("DELETE FROM widgets")
                conn.execute("INSERT INTO widgets(name) VALUES ('pre_fault_row')")
                conn.commit()

        before_data = _capture_db_rowcount_snapshot(s01_settings.db_file)
        run_injector(
            "S01",
            InjectorContext(
                sandbox_root=s01_root,
                app_root=s01_app,
                db_file=s01_settings.db_file,
                backups_root=s01_backups,
            ),
            seed=args.base_seed,
        )
        await s01_tools.execute(
            "apply_schema",
            {"db_path": "data/app.db", "schema_path": "schema.sql"},
            allowed_primitives=s01_tools.primitive_ids,
        )
        verification = await s01_verifier.verify()
        after_data = _capture_db_rowcount_snapshot(s01_settings.db_file)
        s01_apply_schema_check = {
            "recovery_verified": verification.healthy,
            "data_preserved": _data_preserved(before_data, after_data),
            "data_before": before_data,
            "data_after": after_data,
        }

    return {
        "mode": "validate_reference_routes",
        "baseline_app_root": str(baseline_app_root),
        "scenarios": scenario_reports,
        "s01_apply_schema_check": s01_apply_schema_check,
    }


def main() -> int:
    args = _parse_args()
    try:
        if args.mode == "planner_shapes":
            report = asyncio.run(_run_planner_shapes(args))
        elif args.mode == "rescore":
            report = _rescore_existing_report(args)
        elif args.mode == "full_loop_compare":
            report = asyncio.run(_run_full_loop_compare(args))
        elif args.mode == "analyze_top_rank":
            report = _analyze_top_rank(args)
        elif args.mode == "full_loop_outcome_compare":
            report = asyncio.run(_run_full_loop_outcome_compare(args))
        else:
            report = asyncio.run(_run_validate_reference_routes(args))
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(f"real-planner-shapes report={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
