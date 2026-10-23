# Harness Gap Plan

Build plan for every item marked **gap**, **partial**, or **manual** in
`harness/guides/README.md`, `harness/sensors/README.md`, and `harness/governance/README.md`,
plus two issues found during scaffolding. Work is split into checkpoints; each checkpoint
is one commit (or one PR) that leaves `make check` green and can be reverted on its own.

Conventions for every checkpoint:

- **Done when** = commands that must pass before committing.
- **Risky** = needs approval per `harness/governance/README.md` § "Changes that require
  explicit human approval".
- After each checkpoint, flip the matching row's status in the guides/sensors/governance
  catalogue in the same commit.

## Decisions needed before building

| ID | Decision | Blocks | Options |
| --- | --- | --- | --- |
| D1 | What should a security-gate rejection do? | CP1 | **Decided: (b)** — failed attempt with learning penalties (policy −1, class-memory failure), action excluded for the incident, retry from step 5. Rejected alternatives: (a) keep raising; (c) failed attempt without penalties |
| D2 | Where does the live E2E fault-injection regression run? It needs OpenAI, Pinecone, and the local JEV server (`.local/jev`). | CP7 | (a) self-hosted runner with JEV; (b) nightly on a dev host via cron, results committed to `results/`; (c) manual `workflow_dispatch` only |
| D3 | Where does continuous surveillance run? GitHub Actions cannot read a host's `state/`. | CP9 | (a) `harness-watch` service in `docker-compose.yml` sharing the `state` volume; (b) host cron; (c) Claude Code scheduled routine on the host |
| D4 | Which model judges diagnosis/action quality, and who approves the rubric? | CP12 | OpenAI (same provider as runtime) or Claude; rubric owner |
| D5 | Which agent runs LLM code/architecture review on PRs? | CP13 | GitHub Action with an LLM reviewer; selective on risky paths only |
| D6 | Named owners for the roles runtime maintainer / harness maintainer / operator. | CP14 | — |

## Checkpoint 0 — Commit what exists (now)

Two commits, in this order. Both are green on their own: the first was verified in a
clean worktree (`ruff`, `mypy --strict src`, 119 tests).

**0a. Runtime fixes (risky; approved in this session).**
- Gate as README step 9 before registration (`security.gate` event); executor re-checks.
- `persist.policy_store` / `persist.class_memory` events with `status=failure` on failed
  attempts.
- First success of a new class writes its skill section (`render_skills_section(pending=…)`).
- `dynamic_tool.executed` nests executor output under `result`; dashboard reads top-level
  `operation` (fixes the always-`n/a` operation column).

```bash
git checkout -b harness/checkpoint-0
git add src/mi_agent/agent/engine.py src/mi_agent/api/server.py src/mi_agent/class_memory.py \
  src/mi_agent/tools/executor.py scripts/classmem_stage_benchmark.py \
  tests/test_agent_workflow.py tests/test_api_dashboard.py tests/test_class_memory.py \
  tests/test_class_memory_engine.py
git commit -m "fix(runtime): gate as step 9, failure persist events, first-success skills, nested executed payload"
```

**0b. Harness scaffolding (contracts, mappings, sensors, KPIs, docs, CI wiring).**

```bash
git add harness docs/harness tests/harness_scenarios.py tests/test_harness_*.py \
  pyproject.toml Makefile scripts/run_checks.sh .github/workflows/ci.yml
git commit -m "feat(harness): contracts, read-model mappings, invariant sensors, KPIs, governance docs"
```

Done when: `make check` green after each commit.

## Phase 1 — Runtime hardening and repo hygiene

### CP1 — Gate-rejection semantics + benchmark repair (risky; D1 = b approved) — done
- `RecoveryAgent._handle_gate_rejection`: rejection → failed attempt with
  `persist.policy_store` / `persist.class_memory` = `failure`, `incident.recovery_failed`,
  `attempt.result` = `failed`; action excluded; reason fed to HOW. Policy key is the shared
  `base_tool_name(spec)` (`mi_agent.tools.executor`). Non-gate exceptions still propagate.
- Harness: `decision_trace` outcome `gate_rejected`; `security_gate_before_registration`
  also requires a rejection to end the attempt `failed` with no action after it; KPI
  `decision.gate_rejected`; scenario `gate_rejected_then_success`.
- Fixed the pre-existing break in `scripts/classmem_stage_benchmark.py` (`BenchTools`
  lacked `monitor_health` since the post-recovery ping was added).
- Done when: `make check`; `PYTHONPATH=src python scripts/classmem_stage_benchmark.py --incidents 6` exits 0.
- Commit: `fix(runtime): <D1 outcome> on security-gate rejection; repair classmem benchmark`

### CP2 — Pre-commit hook + basic security scan
- Enable ruff `S` (flake8-bandit) rules in `pyproject.toml`; triage existing findings with
  narrow `per-file-ignores` (each with a one-line reason, e.g. `S101` in `tests/`, SQL
  `executescript` in `executor.py` is the gated `run_sql` operation).
- Add `.pre-commit-config.yaml` running `ruff check`, `mypy --strict src harness`, and the
  fast harness tests (`make harness`); add `pre-commit` to dev extras; document
  `pre-commit install` in `README.md` § Validation.
- Done when: `pre-commit run --all-files` and `make check` pass.
- Flip: sensors "Basic security scan" → wired; governance pre-commit row → wired.
- Commit: `chore(harness): pre-commit hook and ruff security rules`

### CP3 — Dependency vulnerability scan (CI + scheduled drift)
- CI job `pip-audit` against the installed `.[dev]` environment.
- `.github/workflows/drift.yml`: weekly schedule running `pip-audit` + `ruff` security rules
  on `main`, opening an issue on failure.
- Done when: both workflows green on a branch push; a deliberately vulnerable pin fails locally.
- Commit: `ci(harness): dependency vulnerability scan and weekly drift workflow`

## Phase 2 — Guides

### CP4 — AGENTS.md and contributor skills
- `AGENTS.md`: repo map, runtime invariants, harness boundary, commands, risky-path list,
  "contracts change in the same commit as the runtime".
- `docs/harness/skills/`: `add-dynamic-tool-operation.md` (schema model + gate allowlist +
  tests + contract enums), `add-workflow-step.md` (engine + contract + `README_STEP` +
  mapping doc), `review-checklist.md`.
- Test: `tests/test_harness_architecture.py` asserts every path referenced in `AGENTS.md`
  exists (prevents guide rot).
- Commit: `docs(harness): AGENTS.md and contributor skills`

### CP5 — Runbooks and policy links
- `docs/runbooks/`: one per fault (`drop_widgets_table`, `delete_db`, `corrupt_db`) with
  symptoms, expected MI behaviour, approved recovery action, manual fallback, escalation;
  plus `security-gate-rejection.md` and `stuck-incident.md` (no recovery after N attempts).
- Link from `harness/guides/README.md` and `docs/THREAT_MODEL.md`.
- Commit: `docs(harness): fault runbooks and policy links`

## Phase 3 — Behaviour lane

### CP6 — Approved scenario fixtures + offline outcome checker
- `harness/scenarios/*.json` (contract `scenario_fixture.schema.v1.json`): fault, repetitions,
  approved outcomes (allowed operations, max attempts, max TTR).
- `python -m harness.events.slice --since/--until/--incident` to cut campaign windows from
  `state/events.jsonl` (referenced by `BASELINE.md` § 4, not yet built).
- `python -m harness.scenarios.check <fixture> <log-slice>` → `sensor_result`
  (`lane=behavior`), comparing `decision_trace`/KPIs against approved outcomes.
- Tests run the checker against `tests/harness_scenarios.py` logs.
- Commit: `feat(harness): scenario fixtures, event slicing, outcome checker`

### CP7 — Live E2E fault-injection runner (depends on D2)
- `scripts/e2e_fault_campaign.py`: for each fixture, wait healthy → `POST /ops/fault/{fault}`
  → wait `incident.closed` (timeout) → slice → check → KPIs; writes `results/<run>/`.
- Workflow per D2 (also runs `pytest -m live`).
- Done when: one full campaign passes on the chosen runner.
- Flip: sensors "E2E fault injection", "Live LLM integration" → wired.
- Commit: `feat(harness): automated E2E fault-injection campaign`

### CP8 — Baseline collection (operator)
- Run CP7 campaign on `main`; fill `docs/harness/BASELINE.md` (no TBD left).
- Commit: `docs(harness): baseline <date>`

## Phase 4 — Continuous lane

### CP9 — Continuous surveillance (depends on D3)
- `harness/metrics/thresholds.json` (min recovery rate, max p90 TTR, max request-error
  rate) and sensor `kpi_thresholds` (`lane=behavior`, `stage=continuous`).
- `python -m harness.watch` runs invariant sensors + KPI thresholds over a rolling window
  and appends `sensor_result` records to `results/harness/sensor_results.jsonl`.
- Deploy per D3. The watcher only reads `state/`.
- Flip: governance continuous rows → wired.
- Commit: `feat(harness): continuous KPI and invariant surveillance`

### CP10 — Runtime budget sensors
- **10a (harness only):** per-step latency from `workflow.step` timestamps (WHY, HOW, JEV,
  execute, verify) with budgets in `thresholds.json`; sensor `step_latency_budget`.
- **10b (runtime, risky):** emit LLM token usage and JEV call counts in `llm.*`/`jev.*`
  steps; sensors `token_budget`, `call_rate_budget`. Contract update in the same commit.
- Commits: `feat(harness): step latency budget sensor`; `feat(runtime): token usage telemetry`

## Phase 5 — Inferential and human lanes

### CP11 — Decision-log sampling for human review
- `python -m harness.review.sample --n 20` builds review packets (incident signature,
  decision traces, outcome) as Markdown; contract `review_verdict.schema.v1.json` for the
  reviewer's verdict (diagnosis correct?, action appropriate?, notes).
- Weekly cadence documented in governance; verdicts stored under `results/reviews/`.
- Commit: `feat(harness): decision-log sampling and review verdicts`

### CP12 — LLM-as-judge on diagnosis and action quality (depends on D4)
- Rubric in `harness/guides/judge_rubric.md` (risky: approval of rubric).
- `python -m harness.review.judge` scores sampled packets → `sensor_result`
  (`sensor_type=inferential`); calibrate against CP11 human verdicts before trusting.
- Commit: `feat(harness): LLM-as-judge for diagnosis and action quality`

### CP13 — Selective LLM code / architecture review in CI (depends on D5)
- PR workflow triggered only on risky paths (governance list); posts review comments;
  advisory, never blocking.
- Add "log-quality reflection" prompt run when a CP7 campaign fails.
- Commit: `ci(harness): selective LLM review on risky paths`

### CP14 — Governance enforcement (depends on D6)
- `.github/CODEOWNERS` for risky paths; `.github/pull_request_template.md` with the
  risky-change checklist and steering-loop link; branch protection (manual GitHub setting,
  recorded in governance doc).
- Commit: `chore(harness): CODEOWNERS and PR template for risky changes`

## Order and dependencies

```text
CP0 ─▶ CP1 (D1) ─▶ CP2 ─▶ CP3
              └──▶ CP4 ─▶ CP5
CP0 ─▶ CP6 ─▶ CP7 (D2) ─▶ CP8 ─▶ CP9 (D3) ─▶ CP10a ─▶ CP10b
CP6 ─▶ CP11 ─▶ CP12 (D4)
CP2 ─▶ CP13 (D5)
CP14 (D6) any time after CP0
```

Phases 1–2 need no external infrastructure and can start immediately after CP0.
