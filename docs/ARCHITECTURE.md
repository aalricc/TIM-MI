# MI Architecture

## Runtime pipeline

1. **Monitor** (`src/mi_agent/agent/monitor.py`)
   - Detects target-health failures and opens recovery handling.
2. **Supervisor** (`src/mi_agent/supervisor/supervisor.py`)
   - Owns incident lock and attempt pacing backoff.
3. **Observation + diagnosis** (`src/mi_agent/agent/diagnose.py`)
   - Collects logs/probes and requests structured diagnosis from LLM.
4. **Planning + selection** (`src/mi_agent/agent/agent.py`)
   - Requests plans from LLM, validates candidates, and selects next action.
5. **Execution** (`src/mi_agent/tools/executor.py`, `src/mi_agent/agent/executor.py`)
   - Executes only registered guarded primitives with rollback support.
6. **Verification** (`src/mi_agent/verifier/verifier.py`)
   - Independently verifies health/integrity/schema and gates success.
7. **Learning** (`src/mi_agent/policy`, `src/mi_agent/skills`)
   - Updates policy values and persists distilled skills.

## LLM architecture

- Mandatory live model: `gpt-5.3-codex`.
- Endpoint source: `MI_LLM_ENDPOINT`.
- Preflight call required before live scenario/test execution.
- Retries for transient failures use exponential backoff + jitter with max delay.
- Permanent configuration/auth/permission errors stop the incident.

## Tooling and safety boundaries

- One primitive registry in `src/mi_agent/tools/executor.py`.
- Strict allowlist + typed validation + least-privilege path guard.
- Unknown actions are rejected and replanned.
- LLM output is never executed as shell/code.

## State and persistence

- `state/policy_store.json`: policy values and lessons.
- `state/global_knowledge/skills.md`: distilled skill knowledge base.
- `state/decision_log.jsonl`: decision telemetry.
- `state/events.jsonl`: incident/execution events.
- `state/failure_taxonomy.json`: failure taxonomy and review state.

## API and observability

- FastAPI app: `src/mi_agent/api/server.py`.
- Core endpoints: `/state`, `/policy`, `/skills`, `/incidents`, `/events/stream`.
- Dashboard payload endpoint: `/dashboard/views` with `learning_curve`, `per_scenario_results`, `skill_lifecycle`, `exploration_rate`, and `last_resort_rate`.

## Evaluation stack

- Scenario harness: `src/mi_agent/eval/scenarios.py`, `injectors.py`, `oracles.py`.
- Live evaluator: `src/mi_agent/eval/runner.py`.
- CLI: `src/mi_agent/eval/cli.py` (`mi-eval`).
- Experiment/report pipeline: `src/mi_agent/eval/experiments.py`, `reporting.py`, `reports.py`.
