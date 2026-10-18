# MI Architecture (Workflow-First)

## Core principle

Built-ins are for diagnosis. Recovery strategy is selected and executed as a per-incident dynamic tool.

## Main components

- `agent/engine.py`
  - Owns incident lifecycle.
  - Enforces monitor → diagnostics → RAG(why) → LLM(why) → RAG(how) → LLM(how) → dynamic execute → verify loop.
- `tools/executor.py`
  - Runs diagnostic probes.
  - Registers temporary dynamic tools.
  - Enforces security-only gate (allowed operation + validated params + sandboxed paths).
  - Persists only verified winning tool into `state/tool_catalog.json`.
- `llm/client.py`
  - OpenAI connectivity is mandatory at runtime.
  - Startup preflight must succeed before monitor loop begins.
  - `diagnose_why`: structured diagnosis (`failure_type`, hypotheses, evidence).
  - `propose_how`: primary dynamic action + ranked alternatives (observability only).
- `verifier/verifier.py`
  - Independent post-action health verification.
- `api/server.py`
  - Observability for status, event stream, tool catalog, policy, dashboard aggregate, and LLM traces.

## Data flow

1. Monitor health endpoint and continuously ingest new error logs.
2. Collect diagnostic evidence (`health_deep`, `db_status`, log tail, integrity check).
3. Build symptom query and retrieve SQLite recovery chunks (RAG #1).
4. Diagnose WHY.
5. Build repair query from diagnosis + latest failure and retrieve focused chunks (RAG #2).
6. Plan HOW as a temporary dynamic tool.
   - LLM returns `primary` + `alternatives[]`; only `primary` executes.
7. Register dynamic tool in session namespace.
8. Security gate validates only security constraints.
9. Execute one dynamic action.
10. Verify recovery.
11. On failure, retain context and retry without hard cap.
12. Persist only the final successful dynamic tool.
13. Update RL policy reward ledger.
14. Persist a learned recovery skill entry in `state/skills.md`.

## Safety model

- Allowed dynamic operations are explicit (`request_backup_restore`, `restart_service`, `run_sql`, `replace_file`).
- Path operations are constrained to configured sandbox / app root.
- No strategy-level rejection in gate; only security validation.

## Persistence

- `state/tool_catalog.json`: only successful final dynamic tools.
- `state/policy_store.json`: cumulative reward stats per action key.
- `state/skills.md`: learned skill playbooks from successful recoveries.
- `state/events.jsonl`: runtime event stream.

## Dashboard surface

- `GET /dashboard` serves the HTML dashboard.
- `GET /agent/dashboard` provides aggregated live panel data.
- `GET /agent/llm-trace` returns sanitized per-incident WHY/HOW trace payloads.
