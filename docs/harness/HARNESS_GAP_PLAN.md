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
| D2 | Where does the live E2E fault-injection regression run? It needs OpenAI, Pinecone, and the local JEV server (`.local/jev`). | CP7 | **Decided: (c)** — manual `workflow_dispatch` only via a self-hosted runner (no schedule) |
| D3 | Where does continuous surveillance run? GitHub Actions cannot read a host's `state/`. | CP9 | **Decided: (a)** — `harness-watch` compose service, `state/` mounted read-only, every 5 min over 24 h; results to `results/harness/sensor_results.jsonl` and the dashboard. Rejected: (b) host cron; (c) Claude Code scheduled routine |
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

### CP2 — Pre-commit hook + basic security scan — done
- ruff `S` (flake8-bandit) rules enabled. Triage of the 681 initial findings:
  `S101` (668 asserts, all in `tests/`) ignored for `tests/**`; `S106` false positive on
  harness param `pass_message` fixed by renaming to `ok_message`; `S324` sha1 content IDs
  in `rag_ingest.py` fixed with `usedforsecurity=False` (digests unchanged); `S104`
  (`main.py` binds `0.0.0.0` for Docker), `S112` (engine skips malformed candidate rows),
  `S311` (seeded report sampling), `S108` (test literal) suppressed inline with reasons.
- `.pre-commit-config.yaml`: local hooks running `ruff check .`, `mypy --strict src`,
  `mypy --strict harness`, `pytest -q` (same as `make check`/CI); `pre-commit` in dev
  extras; `pre-commit install` documented in `README.md` § Validation.
- Done when: `pre-commit run --all-files` and `make check` pass.
- Flip: sensors "Basic security scan" → wired; governance pre-commit row → wired.
- Commit: `chore(harness): pre-commit hook and ruff security rules`

### CP3 — Dependency vulnerability scan (CI + scheduled drift) — done
- Initial audit (Python 3.12, `.[dev,jev,rag]`) found 58 unique advisories (95 report rows)
  in two packages held
  back by caps: `pypdf` 5.9.0 (57; ships in the image via `rag`) and `pytest` 8.4.2
  (CVE-2025-71176). Fixed by raising floors: `pypdf>=6.21.1,<7`, `pytest>=9.0.3,<10`,
  and `pytest-asyncio>=1.3,<2` (first release supporting pytest 9; 1.0–1.2 require
  pytest < 9). Verified: all checks on 3.12 (`.[dev,jev,rag]`) and 3.14 (`.[dev]`), the
  exact new floors, and `pip-audit` clean.
- `pypdf` 6 vs 5 on the 30-PDF corpus: identical text except whitespace on 82 of 254
  pages, which shifts chunk boundaries (663 → 665 records). The local `.venv` already had
  pypdf 6.21.0, whose extraction is byte-identical to 6.21.2, so the pin now matches what
  local ingestion used. The running agent never calls pypdf.
- CI job `audit`; `.github/workflows/drift.yml` (weekly, fresh resolution, `pip-audit` +
  ruff `S`, one tracking issue); `make audit`; `pip-audit` in dev extras. Both
  workflows pass `actionlint` + `shellcheck`; the issue step's create/comment branches
  were exercised against a stub `gh`.
- Done when: CI green on the branch push (`audit` job), and `drift.yml` succeeds on a
  manual `workflow_dispatch` run once the workflow file is on the default branch (GitHub
  only schedules or dispatches workflows that exist there).
- Commit: `ci(harness): dependency vulnerability scan and weekly drift workflow`

### Found during CP3 — fixed (pre-CP7 batch)
- **`pydantic-settings` floor** raised to `>=2.15` (bisected: 2.6.0–2.14.0 fail
  `tests/test_config.py`, 2.15.0 is the first passing). Verified with a lowest-direct
  install of every direct dependency (all tests pass).
- **JEV client SDK.** JEV is a self-hosted open-source model; MI reaches it through the
  third-party alpha SDK `langchain-typesafe`. Pinned exactly (`==0.0.1a3`); CI's test job
  now installs `.[dev,jev,rag]`, so `tests/test_jev_client.py` (previously skipped in CI)
  runs and mypy checks the client against the real SDK. The two type errors are fixed
  (`# type: ignore[import-not-found, unused-ignore]`, `criteria: dict[str, Any]`); mypy is
  clean with and without the extra.
- **Open decision:** replace the SDK with a direct `httpx` client for `jev serve`'s API
  (removes the vendor-alpha dependency and its global `TYPESAFE_*` environment writes).
  Needs the server's API; changes the mandatory JEV path (risky).

## Phase 2 — Guides

### CP4 — AGENTS.md and contributor skills — done
- `AGENTS.md`: setup/commands, repo map, invariants with their enforcing tests/sensors,
  boundaries, risky-change list, conventions, links to skills.
- `docs/harness/skills/`: `add-dynamic-tool-operation.md`, `add-workflow-step.md`,
  `add-contract-field.md`, `review-checklist.md`; every step names the exact code
  location (`path::Symbol`) and the test that catches a missed step.
- `test_agent_guides_reference_existing_code`: every backticked path and `path::Symbol`
  in `AGENTS.md` and the skills must exist (symbols resolved by AST).
- `test_operation_allowlist_is_consistent`: the operation allowlist was duplicated in six
  places (gate, two LLM HOW schema enums, LLM `allowed_operations`, two test sets) plus
  `docs/ARCHITECTURE.md`, with nothing keeping them in sync; any string collection naming
  two or more operations must now equal the gate's set.
- Both tests were mutation-checked (renamed symbol, missing file, operation added to the
  gate only, and gate + pin updated with a stale LLM copy): each fails as intended.
- Commit: `docs(harness): AGENTS.md and contributor skills`

### CP5 — Runbooks and policy links — done
- `docs/runbooks/`: `README.md` (deployment behaviour, common commands, manual takeover,
  post-incident review), `drop_widgets_table.md`, `delete_db.md`, `corrupt_db.md`,
  `security-gate-rejection.md`, `stuck-incident.md`. Symptoms and approved actions come
  from `target/app/main.py`, the executor, and recorded incidents in `state/events.jsonl`.
- `scripts/find_known_good_backup.py` (+ `tests/test_find_known_good_backup.py`): newest
  backup at or before the last healthy heartbeat that passes integrity and has
  `widgets(id, name, created_at)`; read-only; tested on Python 3.12 and via the documented
  `python - < script` invocation. Manual SQL recreate and diagnostic snippets were run
  against real and scenario data before being documented.
- Runbooks are covered by `test_agent_guides_reference_existing_code`.
- Linked from `harness/guides/README.md` and `docs/THREAT_MODEL.md` (three new residual
  risks).

### Found during CP5 — status
Fixed (pre-CP7 batch):
- **Verifier accepted wrong schemas.** The target's deep health now requires the columns
  the widget endpoints read (`target/app/main.py::REQUIRED_WIDGET_COLUMNS`; reports
  `widgets_columns_missing`, error `schema_invalid`). Monitor and verifier both use it, so a
  mis-shaped table now opens an incident and fails verification. Tests pin the column set
  to every `SELECT … FROM widgets` and to the backup finder.
- **Backups copied faulty state, no retention.** `target/backup/backup.sh` copies to a temp
  file, validates the copy with `scripts/find_known_good_backup.py --check`, renames it
  only if valid, and keeps the newest `BACKUP_RETENTION` (360). The backup job runs on
  `python:3.12-slim` with `./scripts` mounted read-only. Tested under `sh` and `dash`
  (the container's `/bin/sh`); not yet run in a container (Docker daemon was not running).
- **Compose default** `MI_CLASSMEM_ENABLED` is now `true` (verified with
  `docker compose config` and no `.env`).
- **CP5 broke CI in a fresh clone.** A runbook cited the gitignored, runtime-written
  `target/app/app.log`, so `test_agent_guides_reference_existing_code` failed outside a
  machine that had run the stack. Runtime-written paths are now exempt, and
  `test_runtime_written_exemptions_are_gitignored` keeps the exemption honest. Each
  pre-CP7 commit was replayed in a clean worktree.

Decided 2026-10-23:
- **Manual repairs credited to MI — fixed.** `RecoveryAgent.run_iteration` re-checks health
  before each retry; a healthy target closes the incident (`monitor.healthy_before_retry`,
  `incident.closed.resolution` = `healthy_before_retry`) with nothing persisted. README,
  `docs/ARCHITECTURE.md`, and the workflow doc's retry rule and § 9 invariants amended
  (approved). New sensor `external_recovery_not_credited`; scenario
  `repaired_before_retry`. Remaining window: a repair landing during an attempt is still
  credited, so runbooks keep "stop MI before a manual repair".
- **Bad learned state** cleared (decision 2026-10-23): `state/` archived to
  `results/state-archive-2026-10-23.tar.gz` (gitignored, verified byte-identical) and
  emptied; MI relearns from harness-driven runs. The archive keeps the pre-harness event
  log for baseline comparison.
- **`target/last_resort`** removed (decision 2026-10-23: unused, never in compose).

## Phase 3 — Behaviour lane

### CP6 — Approved scenario fixtures + offline outcome checker — done
- `harness/scenarios/*.json` (contract `scenario_fixture.schema.v1.json`): fault, repetitions,
  approved outcomes (allowed operations, max attempts, max TTR).
- `python -m harness.events.slice --since/--until/--incident` to cut campaign windows from
  `state/events.jsonl` (wired; referenced by `BASELINE.md` § 4).
- `python -m harness.scenarios.check <fixture> <log-slice>` → `sensor_result`
  (`lane=behavior`), comparing `decision_trace`/KPIs against approved outcomes.
- Tests run the checker against `tests/harness_scenarios.py` logs.
- Commit: `feat(harness): scenario fixtures, event slicing, outcome checker`

Done notes:
- Added `scenario_fixture` v1 contract + fixture set under `harness/scenarios/` for
  `drop_widgets_table`, `delete_db`, and `corrupt_db`.
- Added offline CLIs: `python -m harness.events.slice` and
  `python -m harness.scenarios.check`.
- Added CP6 tests for pass/fail modes, slicer boundaries, and CLI exit codes.
- Updated `docs/harness/BASELINE.md` § 4 to use the built slice/check commands.

### CP7 — Live E2E fault-injection runner (depends on D2)
- `scripts/e2e_fault_campaign.py`: for each fixture, wait healthy → `POST /ops/fault/{fault}`
  → wait `incident.closed` (timeout) → slice → check → KPIs; writes `results/<run>/`.
- Workflow per D2 (also runs `pytest -m live`):
  `.github/workflows/live-e2e-fault-campaign.yml` (`workflow_dispatch` only).
- Done when: one full campaign passes on the chosen runner.
- Flip: sensors "E2E fault injection", "Live LLM integration" → wired.
- Commit: `feat(harness): automated E2E fault-injection campaign`

Implementation notes:
- Campaign script implemented: `scripts/e2e_fault_campaign.py`.
- Manual workflow implemented for D2=(c):
  `.github/workflows/live-e2e-fault-campaign.yml`.
- First full live campaign pass remains the acceptance gate for closing CP7.

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

### CP11 — Decision-log sampling for human review — done
- `python -m harness.review.sample --n 20` builds review packets (incident signature,
  decision traces, outcome) as Markdown; contract `review_verdict.schema.v1.json` for the
  reviewer's verdict (diagnosis correct?, action appropriate?, notes).
- Weekly cadence documented in governance; verdicts stored under `results/reviews/`.
- Commit: `feat(harness): decision-log sampling and review verdicts`

Done notes:
- Added `python -m harness.review.sample --n 20` to build markdown review packets from
  `incident_signature` + `decision_trace` + incident outcomes.
- Added `harness/contracts/review_verdict.schema.v1.json` and wired it into the contract
  registry and contract docs.
- Documented weekly sampling cadence and `results/reviews/` verdict storage in governance
  and the sensor catalogue.

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
