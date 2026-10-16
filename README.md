# MI (Maintenance Intelligence)

MI is a Python 3.12 autonomous recovery agent for a sandboxed SQLite-backed target app.

## What MI does

- Continuously monitors the target service.
- Diagnoses failures via live LLM calls.
- Plans next actions using only registered guarded tools.
- Executes one validated action at a time.
- Verifies recovery with an independent verifier.
- Learns reusable skills and persists them in `state/global_knowledge/skills.md`.

## Runtime guarantees

- No operator escalation path.
- No attempt/hour/wall-clock recovery caps.
- Recovery keeps retrying until verifier success, task cancellation, shutdown, or a permanent LLM error.
- Live model is fixed to `gpt-5.3-codex`.
- Live endpoint is configured by `MI_LLM_ENDPOINT`.
- LLM actions are never executed directly; only registered primitives can run.

## Repository layout

- `src/mi_agent/agent`: monitor, diagnosis, planning, selection, execution loop.
- `src/mi_agent/tools`: primitive registry, schema validation, path guard, rollback snapshots.
- `src/mi_agent/verifier`: independent recovery verification.
- `src/mi_agent/supervisor`: incident lock and attempt pacing backoff.
- `src/mi_agent/skills`: skill distillation, matching, persistence, lifecycle.
- `src/mi_agent/eval`: scenario harness, run summaries, experiments, reporting.
- `src/mi_agent/api`: FastAPI state/telemetry endpoints.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

## Environment variables

Use `.env.example` as canonical names.

Core LLM settings:

- `MI_LLM_MODEL` (must be `gpt-5.3-codex`)
- `MI_LLM_ENDPOINT`
- `MI_LLM_AUTH_MODE`
- `MI_LLM_API_KEY_HEADER`
- `OPENAI_API_KEY` or `MI_OPENAI_API_KEY`
- `MI_LLM_TIMEOUT_SECONDS`
- `MI_LLM_RETRY_MAX_DELAY_SECONDS`
- `MI_LLM_RETRY_JITTER_RATIO`

## Local checks

```bash
pytest -q
ruff check .
mypy --strict src
```

## Live tests (real model)

Default `pytest -q` excludes `live` tests. To run real-model checks:

```bash
OPENAI_API_KEY=<key> MI_LLM_ENDPOINT=<endpoint> pytest -q -m live
```

Live tests run a real preflight request first and fail closed on bad credentials/config.

## Evaluation commands

Offline planning/evaluation summary (no live execution):

```bash
PYTHONPATH=src python -m mi_agent.eval.cli run --seeds 1 --output-dir artifacts/eval --run-id eval_run
PYTHONPATH=src python -m mi_agent.eval.cli experiments --seeds 5 --output-dir artifacts/eval --run-id experiments
```

Live evaluation (real LLM calls + preflight):

```bash
OPENAI_API_KEY=<key> MI_LLM_ENDPOINT=<endpoint> \
PYTHONPATH=src python -m mi_agent.eval.cli run --live --seeds 1 --output-dir artifacts/eval --run-id live_eval
```

## Gate script

Run phase gate checks:

```bash
./scripts/v2_gate.sh <0-8>
```

## Current limitations

- LLM-dependent outcomes can vary run to run; reports always reflect actual executed runs.
- CI does not run live-model tests and does not use secrets.
