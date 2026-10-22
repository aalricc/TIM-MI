# MI Architecture (Workflow-First)

## Core principle

Built-ins are for diagnosis. Recovery strategy is selected and executed as a per-incident dynamic tool.

## Main components

- `agent/engine.py`
  - Owns incident lifecycle.
  - Enforces monitor → diagnostics → RAG(why) → LLM(why) → class-memory lookup (if enabled) → conditional RAG(how)+LLM(how) when fast path is not eligible → JEV select → dynamic execute → verify loop.
- `tools/executor.py`
  - Runs diagnostic probes.
  - Registers temporary dynamic tools.
  - Enforces security-only gate (allowed operation + validated params + sandboxed paths).
  - Persists only verified winning tool into `state/tool_catalog.json`.
- `llm/client.py`
  - OpenAI connectivity is mandatory at runtime.
  - Startup preflight must succeed before monitor loop begins.
  - `diagnose_why`: structured diagnosis (`failure_type`, hypotheses, evidence).
  - `propose_how`: candidate list (`primary` + `alternatives[]`) for JEV selection.
- `class_memory.py`
  - Optional class-memory matching + fast-path eligibility scoring.
  - Fast path never bypasses JEV, security gate, or verifier.
- `decision/client.py`
  - Uses JEV (System One) to select one candidate action or return unusable decision.
- `verifier/verifier.py`
  - Independent post-action health verification.
- `api/server.py`
  - Observability for status, event stream, tool catalog, policy, dashboard aggregate, and LLM traces.

## Data flow

1. Monitor health endpoint and continuously ingest new error logs.
2. Collect diagnostic evidence (`health_deep`, `db_status`, log tail, integrity check).
3. Build symptom query and retrieve SQLite recovery chunks (RAG #1).
4. Diagnose WHY.
5. Class-memory lookup (if enabled): score incident class + fast-path eligibility.
6. If fast-path is not eligible, build repair query from diagnosis + latest failure and retrieve focused chunks (RAG #2).
7. If fast-path is not eligible, plan HOW as candidate actions (`primary` + `alternatives[]`).
8. JEV selects one candidate action.
9. Security gate validates only security constraints before execution.
10. Register selected dynamic tool in session namespace.
11. Execute one dynamic action.
12. Verify recovery.
13. Persist only the final successful dynamic tool.
14. Update RL policy reward ledger.
15. Persist a learned recovery skill entry in `state/skills.md`.
16. Update class-memory store (`state/class_store.json`) when enabled.

If verification fails, retain context and retry from step 5 without hard cap.

## Safety model

- Allowed dynamic operations are explicit (`request_backup_restore`, `restart_service`, `run_sql`, `replace_file`).
- Path operations are constrained to configured sandbox / app root.
- No strategy-level rejection in gate; only security validation.

## Persistence

- `state/tool_catalog.json`: only successful final dynamic tools.
- `state/policy_store.json`: cumulative reward stats per action key.
- `state/skills.md`: learned skill playbooks from successful recoveries.
- `state/class_store.json`: class-memory signatures and action statistics.
- `state/events.jsonl`: runtime event stream.

## Dashboard surface

- `GET /dashboard` serves the HTML dashboard.
- `GET /agent/dashboard` provides aggregated live panel data.
- `GET /agent/llm-trace` returns sanitized per-incident WHY/HOW trace payloads.
