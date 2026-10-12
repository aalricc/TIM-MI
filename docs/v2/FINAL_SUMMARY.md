# MI v2 Final Summary

## What was built

MI v2 was completed across Phases 0–8 with gate-verified implementation for:

- integrity enforcement and v2 gate contract (`scripts/v2_gate.sh`)
- primitive catalog expansion, brokered restore, and layered verifier
- scenario/eval harness + replay runner + baselines + OPE tooling
- state/logging infrastructure and safety shielding
- policy/skills learning (schema, lifecycle, checkpoint import/export, reuse)
- typed loop-free plans, plan cache, model routing, LLM accounting, API observability
- Phase 8 experiment suite, report generator/dashboard views, exportable audit trail,
  docs package, and one-command demo script

All phase task checklists are now ticked in `docs/v2/PROGRESS.md`, and Phase 8 exit gate passes.

## How to run gates

Task gate (Phase 8):

```bash
./scripts/v2_gate.sh 8
```

Phase exit gate (full suite + checklist verification):

```bash
./scripts/v2_gate.sh 8 --exit
```

Both currently end with `GATE PASS` in this environment.

## How to run evaluation and demo

Replay experiment suite:

```bash
PYTHONPATH=src python -m mi_agent.eval.cli experiments --replay \
  --replay-file artifacts/eval/replay.json --seeds 5 \
  --output-dir artifacts/eval --run-id phase8_experiments
```

Replay eval run (scenario-level summary):

```bash
PYTHONPATH=src python -m mi_agent.eval.cli run --replay \
  --replay-file artifacts/eval/replay.json --seeds 1 --curriculum shuffled \
  --held-out-scenarios S17,S18 --held-out-variants --drift \
  --output-dir artifacts/eval --run-id phase8_eval
```

Report + audit exports:

```bash
PYTHONPATH=src python -m mi_agent.eval.cli report \
  --experiments-json artifacts/eval/phase8_experiments.json \
  --eval-json artifacts/eval/phase8_eval.json \
  --decision-log state/decision_log.jsonl \
  --skills-json state/skills.md \
  --output-dir artifacts/eval --run-id phase8_report

PYTHONPATH=src python -m mi_agent.eval.cli audit-export \
  --decision-log state/decision_log.jsonl --event-log state/events.jsonl \
  --output-dir artifacts/eval --run-id phase8_audit
```

One-command demo:

```bash
bash scripts/v2_demo.sh artifacts/eval artifacts/eval/replay.json
```

## Headline results (with limits)

From replay artifacts (`artifacts/eval/phase8_experiments.json` and `artifacts/eval/phase8_eval.json`):

- learning-enabled success mean: `0.9389`
- policy-off success mean: `0.8789`
- skills-off success mean: `0.8589`
- held-out success (learning-enabled): `0.9600`
- drift recovery episodes: `1.8` (learning-enabled) vs `3.2` (policy-off)
- red-team S18 success (learning-enabled): `1.0`
- demo repeat injection actions: `1.722` → `1.111`
- demo repeat-incident speedup: `0.287`

Limits:

- Results are replay-only in this run (offline deterministic mode), not live online LLM measurements.
- FastAPI emits deprecation warnings for `on_event`; functional but should be migrated to lifespan handlers.

## Scenarios not fully solved in current run

From `artifacts/eval/phase8_eval.json` scenario-level success rates:

- `S02`: `0.5`
- `S03`: `0.5`

These are explicitly tracked in `docs/RESULTS.md` and README limitations.

## Open blockers

Tracked in `docs/v2/BLOCKERS.md`:

1. `.git` is read-only in this environment, so required commit creation could not be performed.
2. Live LLM smoke validation was not run here; exact command for human-run live validation is listed.

## Recommended v3 work

- replace replay-only comparisons with full live-key eval runs and confidence reporting
- raise S02/S03 scenario solve rates and add targeted regression tests for those failure modes
- migrate FastAPI startup/shutdown hooks to lifespan API
- extend dashboard from raw JSON/pre views to interactive charts
- integrate audit export into scheduled CI artifact publishing
