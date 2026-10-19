# Rewrite Worklog

## 2026-10-19 — Add class-memory fast path with gated rollout

**Problem.** Repeated incidents could choose inconsistent recovery actions across runs, causing extra retries and latency.
**Approach.** Added canonical action keys, incident normalization/class store, deterministic action-key dedupe, confidence-gated class-memory fast path with mandatory JEV/security gate fallback, in-place class section upserts in `skills.md`, and a repeatable staged benchmark harness; existing security gate and dynamic-tool execution path remain unchanged.
**Files.** `src/mi_agent/class_memory.py`, `src/mi_agent/agent/engine.py`, `src/mi_agent/config.py`, `src/mi_agent/runtime.py`, `src/mi_agent/workflow/models.py`, `tests/test_class_memory.py`, `tests/test_class_memory_engine.py`, `.env.example`, `docker-compose.yml`, `scripts/classmem_stage_benchmark.py`, `docs/classmem_stage_report.md`.
**Check.** `ruff check .` passed; `pytest -q` passed; `python scripts/classmem_stage_benchmark.py --incidents 80 --json` produced stage metrics and gate outcomes.
**Commit message.**
```text
feat: add class-memory fast path with confidence gating

Adds canonical action-key memory, deterministic dedupe, confidence-gated fast path with JEV validation and unchanged security enforcement, plus staged benchmark/reporting for measured rollout gates.
```

## Completed

- Replaced legacy phase-based architecture with workflow-first recovery loop.
- Removed stale eval/skills/policy/supervisor layers that obscured dynamic-tool flow.
- Demoted built-ins to diagnostics-first behavior.
- Added strict startup config gate for RAG/Pinecone.
- Rebuilt docs and env contract to match actual runtime behavior.
- Replaced sprawling test matrix with focused workflow-compliance tests.
- Made Jev and Pinecone-backed RAG mandatory runtime gates (same class as mandatory LLM).
- Inserted explicit JEV decision step between HOW planning and dynamic tool registration.
- Updated retry behavior to continue from repair retrieval/planning (step 5) after failed attempts.

## Current focus

- Keep dynamic tool lifecycle explicit and auditable.
- Preserve only meaningful persisted state (`events`, `policy`, `tool_catalog`).
- Keep docs/specs synchronized to the final 15-step TIM-MI workflow.
