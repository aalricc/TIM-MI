# MI v2 Architecture

This document describes the current MI v2 runtime and evaluation architecture implemented in this repository.

## Runtime pipeline

1. **Monitor loop (`src/mi_agent/agent/monitor.py`)**
   - Pings the target service and triggers incident handling on unhealthy signals.
2. **Incident supervisor (`src/mi_agent/supervisor/supervisor.py`)**
   - Owns incident lock, configurable attempt/hour/time caps, backoff, brokered restore, and last-resort restore.
   - Any cap can be disabled by setting the corresponding limit to `0`.
3. **Observation and diagnosis (`src/mi_agent/agent/diagnose.py`)**
   - Collects logs and probes, computes fingerprints, asks the LLM for diagnosis (or degrades safely).
4. **Planning and selection (`src/mi_agent/agent/agent.py`)**
   - Typed plan validation, plan cache keyed by canonical failure type + state fingerprint, policy ranking,
     shield checks, and execution.
5. **Tool execution (`src/mi_agent/tools/executor.py`)**
   - Allowlisted primitives only, schema validation, snapshot-before-mutate, rollback support by tier.
6. **Verification (`src/mi_agent/verifier/verifier.py`)**
   - Independent multi-layer verifier gates success (connectivity, integrity, schema, RW probe,
     continuity, delayed re-verify).
7. **Learning (`src/mi_agent/policy`, `src/mi_agent/skills`)**
   - Decision logging with propensities, reward updates, skill distillation/retrieval/lifecycle,
     checkpoint import/export.

## Data and state

- `state/policy_store.json`: policy values and lessons.
- `state/skills.md`: learned skills knowledge base and lifecycle metadata.
- `state/decision_log.jsonl`: per-decision records (features, candidates, chosen action, propensities, rewards).
- `state/events.jsonl`: persisted runtime event stream used by audit export.
- `state/failure_taxonomy.json`: vocabulary-only failure taxonomy and review queue.

## API and observability

- Core endpoints: `/state`, `/policy`, `/skills`, `/incidents`, `/events/stream`.
- Phase 7 observability: `/metrics`, `/decisions`, `/eval/runs`, versioned SSE event payloads.
- Phase 8 dashboard views: `/dashboard/views` returns
  - `learning_curve`
  - `per_scenario_results`
  - `skill_lifecycle`
  - `exploration_rate`
  - `last_resort_rate`

## Evaluation and reporting stack

- Harness and scenario model: `src/mi_agent/eval/scenarios.py`, `injectors.py`, `oracles.py`.
- Replay runner: `src/mi_agent/eval/runner.py`.
- CLI entrypoint: `src/mi_agent/eval/cli.py` (`mi-eval ...`).
- Phase 8 experiment suite: `src/mi_agent/eval/experiments.py`.
- Report generator/dashboard payload builder: `src/mi_agent/eval/reporting.py`.
- Audit exporter: `src/mi_agent/eval/audit.py`.

## Security boundaries

- Runtime never imports solvability references.
- Runtime does not read under `docs/`.
- Primitive descriptions remain neutral/mechanical; no scenario instructions.
- Verifier is failure-type agnostic and remains the only success gate.
