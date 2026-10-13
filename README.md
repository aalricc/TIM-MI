# MI (Maintenance Intelligence) v1

Autonomous self-healing agent for a sandboxed SQLite-backed service.

- Agent is LLM-driven (`gpt-5.3-codex` by default) and performs monitoring, diagnosis, planning, action execution, and learning.
- Verifier is independent and is the only source of health truth.
- Supervisor owns lock, backoff, cap, and last-resort restore.
- Backups are isolated from the agent tool layer.
- v1 seeded recovery target: missing SQLite DB file (`db_file_missing`) with dynamic diagnosis.

## Architecture

- `src/mi_agent/agent`: monitor loop, diagnosis, selection, and execution.
- `src/mi_agent/tools`: allowlisted typed primitives with guardrails + snapshots/rollback.
- `src/mi_agent/verifier`: independent deep verification.
- `src/mi_agent/supervisor`: lock/backoff/cap and last-resort client.
- `src/mi_agent/policy`: RL value table + lessons.
- `src/mi_agent/skills`: skill distillation, matching, persistence, demotion/retirement.
- `src/mi_agent/api`: FastAPI API + SSE + static dashboard serving.

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
pytest -q
ruff check .
mypy --strict src
```

## Environment variable names

Use `.env.example` as the canonical list of supported variable names.

- Core model/auth: `MI_LLM_MODEL`, `MI_LLM_ENDPOINT`, `MI_LLM_AUTH_MODE`, `MI_LLM_API_KEY_HEADER`, `OPENAI_API_KEY` (or `MI_OPENAI_API_KEY`)
- Selector controls: `MI_EPSILON_START`, `MI_EPSILON_MIN`, `MI_EPSILON_DECAY`, `MI_EVAL_EPSILON`
- Incident limits: `MI_MAX_ATTEMPTS_PER_INCIDENT`, `MI_MAX_ATTEMPTS_PER_HOUR`, `MI_MAX_INCIDENT_SECONDS`
- Runtime paths: `MI_APP_ROOT`, `MI_SANDBOX_ROOT`, `MI_BACKUPS_ROOT`, `MI_DB_FILE`, `MI_SCHEMA_FILE`, `MI_APP_LOG`

## LLM provider configuration

By default MI uses OpenAI Responses API with bearer auth.

- `MI_LLM_ENDPOINT` default: `https://api.openai.com/v1/responses`
- `MI_LLM_AUTH_MODE` default: `bearer`
- `OPENAI_API_KEY` (or `MI_OPENAI_API_KEY`)

For Azure-style endpoints, use full endpoint URL and API-key header mode:

```bash
export MI_LLM_ENDPOINT='https://<your-resource>.openai.azure.com/openai/responses?api-version=<your-api-version>'
export MI_LLM_AUTH_MODE='api_key'
export MI_LLM_API_KEY_HEADER='api-key'
export OPENAI_API_KEY='<your-azure-api-key>'
```

Set `MI_LLM_MODEL` to your deployment/model name expected by your endpoint.
If your endpoint is throttled, increase `MI_LLM_RETRIES` so MI keeps retrying LLM calls before giving up that attempt.

Retry caps are configurable. Set any cap to `0` to disable that specific limit:

- `MI_MAX_ATTEMPTS_PER_INCIDENT=0`
- `MI_MAX_ATTEMPTS_PER_HOUR=0`
- `MI_MAX_INCIDENT_SECONDS=0`

Candidate rejection telemetry controls:

- `MI_CANDIDATE_REJECTION_SUMMARY_ENABLED=true|false`
- `MI_CANDIDATE_REJECTION_SUMMARY_SAMPLE_LIMIT=5`
- `MI_CANDIDATE_REJECTION_LOG_DECISION_ENABLED=true|false`

Selector evaluation override:

- `MI_EVAL_EPSILON=0` (deterministic exploit-only selection for evaluation)

## Run demo (Docker Compose)

```bash
docker compose up --build
```

Services:
- Target app: `http://localhost:8080`
- MI API + dashboard: `http://localhost:8090`
- Last-resort restore service: `http://localhost:8070`

## Demo: trigger `db_file_missing`

1. Start stack.
2. Open dashboard: `http://localhost:8090`
3. Manually delete DB inside target container:

```bash
docker exec -it mi-target-app rm /target/app/data/app.db
```

4. MI detects failure on next monitor ping (default 5s), recovers, and learns a skill.
5. Delete DB again with the same command.
6. Observe MI match and run learned skill for faster recovery.

## API quick reference

- `GET /state`: current incident + epsilon settings.
- `GET /policy`: RL action values and lessons.
- `GET /skills`: learned skills + stats.
- `GET /incidents`: incident history events.
- `GET /events/stream`: SSE event stream.

## Safety properties

- LLM output is never executed as code/shell.
- LLM may only choose allowlisted primitive IDs with schema-validated parameters.
- Mutating actions snapshot before execution and support rollback.
- Paths are sandbox-scoped and backup paths are hard-denied (incl. traversal/symlink attempts).
- Agent cannot self-report success; verifier result gates resolution.

## v1 limitations

- Dynamic failure typing is inferred by diagnosis output and persisted in policy/skills.
- LLM client uses a minimal Responses API wrapper and expects compatible response JSON.
- Dashboard is intentionally minimal static HTML.
- Last-resort restore in Compose is HTTP-based; local tests may also use local restorer.

## MI v2 artifacts (current)

- Spec and phased build record: `docs/v2/V2_SPEC.md`, `docs/v2/PROGRESS.md`.
- Baseline and baselines: `docs/v2/V2_BASELINE.md`, `docs/v2/BASELINES.md`.
- Threat model: `docs/THREAT_MODEL.md`.
- Architecture overview: `docs/ARCHITECTURE.md`.
- Results snapshot: `docs/RESULTS.md`.

## MI v2 replay evaluation quickstart

```bash
PYTHONPATH=src python -m mi_agent.eval.cli experiments --replay \
  --replay-file artifacts/eval/replay.json --seeds 5 \
  --output-dir artifacts/eval --run-id phase8_experiments

PYTHONPATH=src python -m mi_agent.eval.cli run --replay \
  --replay-file artifacts/eval/replay.json --seeds 1 --curriculum shuffled \
  --held-out-scenarios S17,S18 --held-out-variants --drift \
  --output-dir artifacts/eval --run-id phase8_eval

PYTHONPATH=src python -m mi_agent.eval.cli report \
  --experiments-json artifacts/eval/phase8_experiments.json \
  --eval-json artifacts/eval/phase8_eval.json \
  --decision-log state/decision_log.jsonl \
  --skills-json state/skills.md \
  --output-dir artifacts/eval --run-id phase8_report

PYTHONPATH=src python -m mi_agent.eval.cli audit-export \
  --decision-log state/decision_log.jsonl \
  --event-log state/events.jsonl \
  --output-dir artifacts/eval --run-id phase8_audit
```

## Oracle gate (S01-S10)

Run the reference-route oracle validation across S01-S10:

```bash
PYTHONPATH=src python scripts/real_planner_shapes.py \
  --mode validate_reference_routes \
  --scenarios S01,S02,S03,S04,S05,S06,S07,S08,S09,S10 \
  --baseline-app-root target/app \
  --output artifacts/eval/reference_route_validation_s01_s10.json
```

## Full-loop baseline command (reference)

```bash
PYTHONPATH=src python scripts/real_planner_shapes.py \
  --mode full_loop_outcome_compare \
  --runs 10 \
  --scenarios S01,S02,S03,S04,S05,S06,S07,S08,S09,S10 \
  --baseline-app-root target/app \
  --acceptable-remediations artifacts/eval/acceptable_remediations_s01_s10.json \
  --output artifacts/eval/full_loop_outcome_compare_s01_s10_runs10_final.json
```

## Known issues

- Historical `full_loop_outcome_compare` numbers (`15%` flag-off / `27%` flag-on) are not directly comparable after harness fixes.
- Two root causes in older harness runs were:
  - Full-loop sandboxes did not consistently seed verified pre-injection backups.
  - `correct_action_in_candidates` counted acceptable actions anywhere in planner-ranked plans, while runtime executes step 1 candidates only.
- Remaining work is tracked in code/tests for scenario-level validity checks under "do-nothing/wait-only" stubs.

## MI v2 live scenario injection (one-by-one)

Use this to inject a specific scenario into a running sandbox target (v1-style manual flow).

```bash
PYTHONPATH=src python -m mi_agent.eval.cli scenario --id S01 --seed 11

# explicit paths (useful outside the default /target layout)
PYTHONPATH=src python -m mi_agent.eval.cli scenario \
  --id S08 \
  --sandbox-root /target \
  --app-root /target/app \
  --db-file /target/app/data/app.db \
  --backups-root /target/backups

# run a sequence (defaults to S01..S18) and log json lines
bash scripts/v2_scenarios.sh artifacts/eval/scenario_injections.jsonl

# host mode auto-falls back to ./target if /target is not writable

# optional pacing between injections (seconds)
MI_SCENARIO_WAIT_SECONDS=20 bash scripts/v2_scenarios.sh artifacts/eval/scenario_injections.jsonl

# optional: disable per-scenario baseline reset (default reset mode is "soft")
MI_SCENARIO_RESET_MODE=none bash scripts/v2_scenarios.sh artifacts/eval/scenario_injections.jsonl
```

## Current limitations (MI v2 replay run)

- Results currently are replay-only (offline deterministic mode), not live-key online runs.
- In the current scenario-level replay report, the agent does not fully solve:
  - `S02`
  - `S03`
