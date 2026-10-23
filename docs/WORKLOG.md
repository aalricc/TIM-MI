# Rewrite Worklog

## 2026-10-23 — Harness foundation, runtime-step hardening, and campaign automation

**Problem.** After the workflow-first rewrite, the project needed a diagnostics harness with enforceable runtime invariants, stronger security/retry semantics in the engine, safer infra/dependency posture, and repeatable end-to-end campaign coverage.
**Approach.** Added harness contracts/mappings/sensors/KPIs and governance docs, enforced step ordering and failure semantics in runtime (gate/registration/execution/verify/persistence), hardened infra around backup/deep-health behavior and dependency pinning/scans, expanded contributor runbooks/architecture notes, and added scenario fixtures plus automated fault-injection campaign scripts.
**Impact.** Runtime behavior is now more observable and structurally constrained, harness outputs are schema-backed and testable, CI/security posture is tighter, and E2E fault validation is reproducible with less manual effort.

**Commits included (2026-10-23).**
- `bc6cafe4` Updated 22nd Oct worklog.
- `8c8ca5ef` Updated architecture diagram with harness addition.
- `b3ca2d56` Fix runtime: gate as step 9, failure persist events, first-success skills, nested executed payload.
- `b7c20611` Feat harness: contracts, read-model mappings, invariant sensors, KPIs, governance docs.
- `6249d9be` Fix runtime: security-gate rejection treated as failed attempt with learning penalties; repair classmem benchmark.
- `7566b343` Chore harness: pre-commit hook and ruff security rules.
- `32fcd5a6` CI harness: dependency vulnerability scan and weekly drift workflow; raise pypdf/pytest floors.
- `585f7d62` Docs harness: AGENTS.md, contributor skills, and guide/allowlist consistency tests.
- `dcb86ba4` Docs harness: fault runbooks, known-good backup finder, threat-model residual risks.
- `ebd39786` Test harness: exempt runtime-written paths from guide reference check (fresh-clone CI fix).
- `e2643724` Fix deps: pin JEV client SDK, raise pydantic-settings floor, run JEV tests and mypy in CI.
- `f6eca3bb` Fix target: deep health requires widget columns read by endpoints.
- `d7c77be2` Fix infra: validate backups before keeping them, add retention; default class memory on.
- `0ed94ad5` Docs harness: update runbooks, threat model, and plan for pre-CP7 fixes.
- `5728a607` Chore: remove unused target/last_resort; record state reset in plan.
- `a98d4ff1` Fix runtime: re-check health before each retry; close externally repaired incidents without learning.
- `56150113` Docs harness: handoff brief for continuing the gap plan.
- `dc1ee294` Feat harness: scenario fixtures, event slicing, outcome checker.
- `31729d94` Feat harness: automated E2E fault-injection campaign.

## 2026-10-22 — Post-recovery close behavior and workflow timeline stabilization

**Problem.** Incidents were not consistently closed immediately after a healthy post-recovery ping, and dashboard/test visibility needed a more stable, workflow-native event stream across local/CI runs.
**Approach.** Standardized post-recovery close behavior in the agent/runtime path, added canonical `workflow.step` publishing across attempts/path modes, made dashboard timeline rendering prefer workflow-step semantics, tightened startup/config gates (including mandatory class-memory flags), and stabilized API tests around timeline and startup-event variability.
**Impact.** Recovery completion state is more deterministic, timeline observability is clearer (`fast_path` vs `full_loop`), startup/runtime guardrails are stricter, and CI-facing dashboard assertions are less flaky.

**Commits included (2026-10-22).**
- `9071ab4a` Fix recovery: close incident immediately after healthy post-recovery ping; add workflow-step tracing and related tests.
- `2ab0f61a` Fix runtime/dashboard: post-recovery close semantics, monitor/timeline gating updates, class-memory startup enforcement, and expanded tests.
- `62f4de8b` Stabilize API dashboard timeline/startup assertions for CI.
- `d0ee245d` Update prior (2026-10-21) worklog entry.

## 2026-10-21 — RAG corpus/eval hardening, docs alignment, and JEV trace clarity

**Problem.** RAG quality and observability needed stronger evaluation coverage, cleaner corpus preparation, stricter type/lint compliance for optional deps, and clearer docs/decision traces.
**Approach.** Tuned RAG query rewrite defaults, expanded regression/schema eval cases, added a cleaned `sqlite-docs-v2` corpus pipeline plus evaluation docs/glossary, fixed strict mypy/lint issues around optional RAG imports and incident context typing, aligned architecture docs with README fast-path semantics, and added JEV shadow extraction in the decision client for better traceability.
**Impact.** Better retrieval/eval confidence, cleaner CI/static-check posture, improved operational docs consistency, and richer JEV decision observability.

**Commits included (2026-10-21).**
- `025cd370` Tune RAG query rewrite defaults and eval coverage.
- `9b503dfe` Add regression eval cases.
- `4c181897` Fix strict mypy issues in RAG eval and engine.
- `bf10e3af` Apply SIM108 cleanup in `rag_eval` metadata handling.
- `3fa763cf` Fix mypy strict issues for optional RAG deps and incident fields.
- `d8c007bd` Fix strict typing and lint for optional RAG imports.
- `d17aa51b` Add 6 schema-related eval cases.
- `26e27d91` Add cleaned corpus pipeline (`sqlite-docs-v2`), PDF/schema eval cases, and corpus evaluation doc.
- `f589298c` + `0421e359` Ignore generated eval results.
- `f6e1f322` Add error-message glossary source doc.
- `14416a8c` Align architecture flow docs with README fast-path semantics.
- `517355b5` Add JEV shadow extraction in decision client.

## 2026-10-20 — Fix class-memory fast-path JEV 422 for single-candidate decisions

**Problem.** Fast path became eligible but still fell back to full loop because JEV returned `422 Unprocessable Entity` when only one candidate action was present (`A choice needs at least two options`).
**Approach.** Kept JEV/security gating intact and fixed request shaping in `JevDecisionClient`: only include `select_action` Choice when there are two or more candidates; for one candidate, score usability/risk and bind selection to that candidate. Also kept class-memory fallback retries in the engine and tightened class-memory signature/action canonicalization to improve stable reuse.
**Files.** `src/mi_agent/decision/client.py`, `src/mi_agent/agent/engine.py`, `src/mi_agent/class_memory.py`, `tests/test_jev_client.py`, `tests/test_class_memory_engine.py`, `tests/test_class_memory.py`.
**Check.** `ruff check src/mi_agent/agent/engine.py src/mi_agent/class_memory.py src/mi_agent/decision/client.py tests/test_class_memory.py tests/test_class_memory_engine.py tests/test_jev_client.py` passed; `pytest -q` passed; live smoke recovered 3/3 with `source=class_memory`.
**Commit message.**
```text
fix: class-memory fast-path + single-candidate JEV

Prevents JEV 422 on class-memory single-candidate decisions by omitting Choice when only one option exists, while preserving JEV/security gating and enabling stable fast-path recovery.
```

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

## 2026-10-18 — Rewrite main into workflow-first skeleton (`00011165`)

**Problem.** The previous shape mixed legacy phase paths and made the actual recovery workflow hard to reason about, test, and operate.
**Approach.** Rewrote main/runtime wiring around the real workflow skeleton, aligned core modules (workflow, tools, verifier, decision, state), and refreshed docs/tests so runtime behavior matched the architecture.
**Files.** Large repo-wide rewrite in commit `00011165e6b07c5f415d9bdcecc1ee616f9ece6e` (entry points, workflow/state, tools, config, docs, CI/test surface).
**Check.** Commit established the new baseline used by later class-memory and fast-path fixes.

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
