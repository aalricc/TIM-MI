from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from mi_agent.config import MISettings
from mi_agent.eval.audit import export_audit_trail
from mi_agent.eval.baselines import compute_baselines, render_baselines_markdown
from mi_agent.eval.experiments import (
    ExperimentSuiteConfig,
    run_replay_experiments,
    write_experiment_reports,
)
from mi_agent.eval.injectors import InjectorContext, run_injector
from mi_agent.eval.learning_curve import run_learning_curve_smoke
from mi_agent.eval.ope import estimate_off_policy, load_decision_log
from mi_agent.eval.promotion import evaluate_promotion
from mi_agent.eval.replay import ReplayLLMClient
from mi_agent.eval.reporting import generate_phase8_report
from mi_agent.eval.reports import write_eval_reports
from mi_agent.eval.runner import EvalRunConfig, build_episode_plan, run_replay_eval
from mi_agent.eval.scenarios import DEFAULT_SCENARIOS, SCENARIO_BY_ID


def _csv_to_set(raw: str) -> frozenset[str]:
    if raw.strip() == "":
        return frozenset()
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return frozenset(values)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mi-eval")
    subparsers = parser.add_subparsers(dest="command", required=True)

    smoke = subparsers.add_parser("smoke", help="run lightweight harness smoke checks")
    smoke.add_argument("--replay", action="store_true", help="use replay mode")

    run = subparsers.add_parser("run", help="run replay evaluation episodes")
    run.add_argument("--replay", action="store_true", help="use replay mode")
    run.add_argument("--replay-file", type=Path, default=Path("artifacts/eval/replay.json"))
    run.add_argument("--seeds", type=int, default=1)
    run.add_argument("--curriculum", choices=("ordered", "shuffled"), default="ordered")
    run.add_argument("--held-out-scenarios", default="")
    run.add_argument("--held-out-variants", action="store_true")
    run.add_argument("--drift", action="store_true")
    run.add_argument("--shuffle-seed", type=int, default=17)
    run.add_argument("--base-seed", type=int, default=7)
    run.add_argument("--output-dir", type=Path, default=Path("artifacts/eval"))
    run.add_argument("--run-id", default="eval_run")

    baseline = subparsers.add_parser("baseline", help="compute baseline metrics")
    baseline.add_argument("--seeds", type=int, default=1)
    baseline.add_argument("--curriculum", choices=("ordered", "shuffled"), default="ordered")
    baseline.add_argument("--held-out-scenarios", default="")
    baseline.add_argument("--held-out-variants", action="store_true")
    baseline.add_argument("--drift", action="store_true")
    baseline.add_argument("--shuffle-seed", type=int, default=17)
    baseline.add_argument("--base-seed", type=int, default=7)
    baseline.add_argument("--output", type=Path, default=Path("artifacts/eval/BASELINES.md"))

    ope = subparsers.add_parser("ope", help="run offline policy evaluation estimators")
    ope.add_argument("--decision-log", type=Path, default=Path("state/decision_log.jsonl"))
    ope.add_argument(
        "--target-policy",
        choices=("behavior", "uniform", "greedy"),
        default="uniform",
    )

    promote = subparsers.add_parser("promote", help="run OPE-based promotion check")
    promote.add_argument("--incumbent-log", type=Path, required=True)
    promote.add_argument("--candidate-log", type=Path, required=True)
    promote.add_argument(
        "--target-policy",
        choices=("behavior", "uniform", "greedy"),
        default="uniform",
    )
    promote.add_argument("--metric", choices=("ips", "dr"), default="dr")
    promote.add_argument("--confidence", type=float, default=0.95)

    learning_smoke = subparsers.add_parser(
        "learning-smoke",
        help="run replay-only learning-curve smoke check",
    )
    learning_smoke.add_argument("--replay", action="store_true", help="use replay mode")
    learning_smoke.add_argument("--episodes", type=int, default=60)
    learning_smoke.add_argument("--seed", type=int, default=7)

    experiments = subparsers.add_parser(
        "experiments",
        help="run Phase 8 replay experiments with confidence intervals and ablations",
    )
    experiments.add_argument("--replay", action="store_true", help="use replay mode")
    experiments.add_argument(
        "--replay-file", type=Path, default=Path("artifacts/eval/replay.json")
    )
    experiments.add_argument("--seeds", type=int, default=5)
    experiments.add_argument("--shuffle-seed", type=int, default=17)
    experiments.add_argument("--base-seed", type=int, default=7)
    experiments.add_argument("--held-out-scenarios", default="S17,S18")
    experiments.add_argument(
        "--no-held-out-variants",
        action="store_false",
        dest="held_out_variants",
    )
    experiments.add_argument("--no-drift", action="store_false", dest="drift")
    experiments.add_argument("--confidence", type=float, default=0.95)
    experiments.add_argument("--output-dir", type=Path, default=Path("artifacts/eval"))
    experiments.add_argument("--run-id", default="phase8_experiments")
    experiments.set_defaults(held_out_variants=True, drift=True)

    scenario = subparsers.add_parser(
        "scenario",
        help="inject one live scenario fault into a target app sandbox",
    )
    scenario.add_argument("--id", dest="scenario_id", required=True)
    scenario.add_argument("--seed", type=int, default=7)
    scenario.add_argument("--sandbox-root", type=Path, default=None)
    scenario.add_argument("--app-root", type=Path, default=None)
    scenario.add_argument("--db-file", type=Path, default=None)
    scenario.add_argument("--backups-root", type=Path, default=None)
    scenario.add_argument("--json", action="store_true", help="emit json payload")

    report = subparsers.add_parser(
        "report",
        help="generate a Phase 8 report with dashboard view payloads",
    )
    report.add_argument(
        "--experiments-json",
        type=Path,
        default=Path("artifacts/eval/phase8_experiments.json"),
    )
    report.add_argument("--eval-json", type=Path, default=None)
    report.add_argument("--decision-log", type=Path, default=Path("state/decision_log.jsonl"))
    report.add_argument("--skills-json", type=Path, default=Path("state/skills.md"))
    report.add_argument("--output-dir", type=Path, default=Path("artifacts/eval"))
    report.add_argument("--run-id", default="phase8_report")

    audit = subparsers.add_parser(
        "audit-export",
        help="export decisions, actions, and verifier results as an audit trail",
    )
    audit.add_argument("--decision-log", type=Path, default=Path("state/decision_log.jsonl"))
    audit.add_argument("--event-log", type=Path, default=Path("state/events.jsonl"))
    audit.add_argument("--output-dir", type=Path, default=Path("artifacts/eval"))
    audit.add_argument("--run-id", default="phase8_audit_trail")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv_items = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(argv_items)

    if args.command == "smoke":
        if not args.replay:
            print("smoke requires --replay in CI gate mode", file=sys.stderr)
            return 2
        print("mi-eval smoke --replay: PASS")
        return 0

    if args.command == "run":
        if not args.replay:
            print("run requires --replay for offline evaluation", file=sys.stderr)
            return 2

        client = ReplayLLMClient(args.replay_file)
        config = EvalRunConfig(
            seeds=args.seeds,
            curriculum=args.curriculum,
            held_out_scenarios=_csv_to_set(args.held_out_scenarios),
            held_out_variants=args.held_out_variants,
            drift=args.drift,
            shuffle_seed=args.shuffle_seed,
            base_seed=args.base_seed,
        )
        summary = run_replay_eval(config, client)
        report_json, report_md = write_eval_reports(summary, args.output_dir, run_id=args.run_id)
        split_counts = summary["split_counts"]
        print(
            "mi-eval run: "
            f"episodes={summary['episodes']} "
            f"success_rate={summary['success_rate']:.3f} "
            f"drift={summary['drift_episodes']} "
            f"split_counts={split_counts} "
            f"report_json={report_json} "
            f"report_md={report_md}"
        )
        return 0

    if args.command == "baseline":
        plan = build_episode_plan(
            scenarios=DEFAULT_SCENARIOS,
            seeds_per_scenario=args.seeds,
            curriculum=args.curriculum,
            held_out_scenarios=_csv_to_set(args.held_out_scenarios),
            include_held_out_variants=args.held_out_variants,
            drift_mode=args.drift,
            shuffle_seed=args.shuffle_seed,
            base_seed=args.base_seed,
        )
        baselines = compute_baselines(plan)
        generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        command = " ".join(["mi-eval", *argv_items])
        markdown = render_baselines_markdown(
            baselines,
            source_summary={
                "episodes": len(plan),
                "curriculum": args.curriculum,
                "seeds": args.seeds,
                "held_out_scenarios": sorted(_csv_to_set(args.held_out_scenarios)),
                "held_out_variants": args.held_out_variants,
                "drift": args.drift,
            },
            command=command,
            generated_at=generated_at,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(markdown, encoding="utf-8")
        print(f"mi-eval baseline: output={args.output}")
        return 0

    if args.command == "ope":
        entries = load_decision_log(args.decision_log)
        result = estimate_off_policy(entries, target_policy=args.target_policy)
        print(json.dumps(result, sort_keys=True))
        return 0

    if args.command == "promote":
        incumbent_entries = load_decision_log(args.incumbent_log)
        candidate_entries = load_decision_log(args.candidate_log)
        incumbent_ope = estimate_off_policy(incumbent_entries, target_policy=args.target_policy)
        candidate_ope = estimate_off_policy(candidate_entries, target_policy=args.target_policy)
        decision = evaluate_promotion(
            incumbent_ope,
            candidate_ope,
            metric=args.metric,
            confidence=args.confidence,
        )
        print(
            json.dumps(
                {
                    "target_policy": args.target_policy,
                    "metric": args.metric,
                    "incumbent": incumbent_ope,
                    "candidate": candidate_ope,
                    "decision": decision,
                },
                sort_keys=True,
            )
        )
        return 0 if bool(decision["promote"]) else 1

    if args.command == "learning-smoke":
        if not args.replay:
            print("learning-smoke requires --replay for offline evaluation", file=sys.stderr)
            return 2
        summary = run_learning_curve_smoke(episodes=args.episodes, seed=args.seed)
        print(json.dumps(summary, sort_keys=True))
        return 0

    if args.command == "experiments":
        if not args.replay:
            print("experiments requires --replay for offline evaluation", file=sys.stderr)
            return 2

        client = ReplayLLMClient(args.replay_file)
        suite_config = ExperimentSuiteConfig(
            seeds=args.seeds,
            curriculum="shuffled",
            held_out_scenarios=_csv_to_set(args.held_out_scenarios),
            held_out_variants=bool(args.held_out_variants),
            drift=bool(args.drift),
            shuffle_seed=args.shuffle_seed,
            base_seed=args.base_seed,
            confidence=args.confidence,
        )
        summary = run_replay_experiments(suite_config, client)
        report_json, report_md = write_experiment_reports(
            summary,
            args.output_dir,
            run_id=args.run_id,
            command=" ".join(["mi-eval", *argv_items]),
        )
        print(
            "mi-eval experiments: "
            f"seeds={args.seeds} "
            f"report_json={report_json} "
            f"report_md={report_md}"
        )
        return 0

    if args.command == "scenario":
        scenario_id = args.scenario_id.strip()
        if scenario_id not in SCENARIO_BY_ID:
            known = ",".join(sorted(SCENARIO_BY_ID))
            print(f"unknown scenario id: {scenario_id} (known: {known})", file=sys.stderr)
            return 2

        settings = MISettings()
        context = InjectorContext(
            sandbox_root=(args.sandbox_root or settings.sandbox_root),
            app_root=(args.app_root or settings.app_root),
            db_file=(args.db_file or settings.db_file),
            backups_root=(args.backups_root or settings.backups_root),
        )

        try:
            injection_result = run_injector(scenario_id, context, seed=args.seed)
        except (FileNotFoundError, PermissionError, OSError, ValueError) as exc:
            print(f"mi-eval scenario: failed id={scenario_id} error={exc}", file=sys.stderr)
            return 1

        payload = {
            "scenario_id": injection_result.scenario_id,
            "seed": args.seed,
            "hidden_label": injection_result.hidden_label,
            "notes": injection_result.notes,
            "sandbox_root": str(context.sandbox_root),
            "app_root": str(context.app_root),
            "db_file": str(context.db_file),
            "backups_root": str(context.backups_root),
        }
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            print(
                "mi-eval scenario: "
                f"id={payload['scenario_id']} "
                f"seed={payload['seed']} "
                f"label={payload['hidden_label']} "
                f"notes={payload['notes']} "
                f"app_root={payload['app_root']} "
                f"db_file={payload['db_file']}"
            )
        return 0

    if args.command == "report":
        _, report_json, report_md = generate_phase8_report(
            experiments_json=args.experiments_json,
            output_dir=args.output_dir,
            run_id=args.run_id,
            decision_log=args.decision_log,
            skills_json=args.skills_json,
            eval_json=args.eval_json,
        )
        print(
            "mi-eval report: "
            f"report_json={report_json} "
            f"report_md={report_md}"
        )
        return 0

    if args.command == "audit-export":
        payload, json_path, jsonl_path = export_audit_trail(
            decision_log_path=args.decision_log,
            event_log_path=args.event_log,
            output_dir=args.output_dir,
            run_id=args.run_id,
        )
        counts = payload.get("counts", {})
        print(
            "mi-eval audit-export: "
            f"counts={counts} "
            f"json={json_path} "
            f"jsonl={jsonl_path}"
        )
        return 0

    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
