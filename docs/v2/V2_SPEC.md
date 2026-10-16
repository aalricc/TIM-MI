# MI v2 Specification (Current)

## Objective

Operate an always-online autonomous recovery agent that:

- observes target failures
- reasons via live LLM calls
- executes only guarded registered actions
- verifies outcomes independently
- learns from verifier-confirmed recoveries

## Core requirements

1. **Autonomy**
   - No operator handoff path in the runtime loop.
   - No recovery stop caps based on attempts or elapsed time.

2. **LLM contract**
   - Mandatory model: `gpt-5.3-codex`.
   - Endpoint source: `MI_LLM_ENDPOINT`.
   - Preflight required before live scenarios/tests.
   - Transient errors: retry with exponential backoff + jitter.
   - Permanent auth/permission/config errors: fail closed with explicit remediation.

3. **Action safety**
   - One primitive registry in `src/mi_agent/tools/executor.py`.
   - Strict allowlist and parameter schema validation.
   - Least-privilege execution and rollback where supported.

4. **Verification and learning**
   - Verifier is the only success gate.
   - On verifier-confirmed recovery, distilled skills are persisted to `state/global_knowledge/skills.md`.

5. **Evaluation**
   - Offline suite (`pytest -q`) excludes live tests.
   - Live suite (`pytest -q -m live`) uses real model calls and fails closed when credentials/config are invalid.
   - Reports must reflect actual run outcomes; if live did not execute, report as not run.
