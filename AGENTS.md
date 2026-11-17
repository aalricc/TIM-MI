# AGENTS.md

Working agreement for coding agents and contributors in this repository. Every path and
`path::Symbol` reference below is checked by
`tests/test_harness_architecture.py::test_agent_guides_reference_existing_code`.

## Work in progress

The harness gap plan is being built checkpoint by checkpoint. Before doing harness work,
read `docs/harness/HANDOFF.md` (status, next checkpoint, working method, gotchas).

Current checkpoint status (2026-10-26): CP0–CP10 done. Baseline and comparison campaigns
are in `docs/harness/BASELINE.md`; continuous surveillance runs as the compose
`harness-watch` service (`harness/watch.py`, D3 = a) with step latency, token (p90 ≤ 126000)
and JEV call budgets; CP11 (decision-log review packets), CP12 (LLM judge + calibration),
CP13 (selective risky-path PR LLM review + failed-campaign reflection), and CP14
(governance enforcement: CODEOWNERS + PR template + branch-protection runbook) are done.
D2 is manual `workflow_dispatch` on a self-hosted runner.
2026-11-17 deploy-readiness pass: WHY/HOW prompts rewritten (role + evidence guide + exact
operation params, no hints), simulated faults leave no trace MI can read, deep health
includes a rolled-back write check, `main` protected by the three CI checks
(`harness/governance/README.md`), landing page in `site/`, demo recorder
`scripts/record_demo.py`.

## What this repo is

MI is a diagnostics-first recovery agent for a SQLite-backed target service. The runtime
loop (16 steps, retry from step 5) is defined in `README.md` and implemented in
`src/mi_agent/agent/engine.py::RecoveryAgent`. The harness (guides + sensors around the
runtime) is in `harness/` and `docs/harness/`; start with
`docs/harness/HARNESS_ARCHITECTURE.md`.

## Setup and commands

Python 3.12 is the CI and Docker version (`pyproject.toml` allows ≥ 3.12).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'          # add ,jev,rag,trace for the full image dependency set
pre-commit install               # once per clone
make check                       # ruff, pytest, mypy --strict src, mypy --strict harness
make harness                     # harness tests + mypy harness only
make audit                       # pip-audit dependency scan
pytest -q -m live                # opt-in; needs real OpenAI credentials
```

For real end-to-end execution (target + MI + live campaign), use the full extras:

```bash
pip install -e '.[dev,jev,rag,trace]'
```

Node.js is not required for this repo's runtime or tests (dashboard is static HTML).

`pytest -q` excludes `live` tests. The pre-commit hook runs the same commands as `make check`
and CI (`.pre-commit-config.yaml`, `.github/workflows/ci.yml`); commits need the virtualenv
active.

## Repository map

| Path | Contents |
| --- | --- |
| `src/mi_agent/agent/engine.py` | recovery loop, `workflow.step` events, persistence steps |
| `src/mi_agent/tools/executor.py` | diagnostics, dynamic-tool lifecycle, security gate |
| `src/mi_agent/tools/schemas.py` | per-operation parameter models |
| `src/mi_agent/class_memory.py` | incident classes, fast path, `state/skills.md` sections |
| `src/mi_agent/decision/client.py` | JEV action selection |
| `src/mi_agent/llm/client.py` | WHY / HOW LLM calls |
| `src/mi_agent/llm/tracing.py` | optional LangSmith tracing of the LLM calls |
| `src/mi_agent/api/server.py`, `dashboard/index.html` | observability API and dashboard |
| `harness/contracts/` | versioned JSON Schemas for harness records |
| `harness/mappings/runtime.py` | read-only mapping from runtime outputs to records |
| `harness/sensors/runtime_invariants.py` | event-log invariant sensors |
| `harness/metrics/kpis.py` | KPI computation |
| `harness/watch.py` | continuous surveillance (invariants + `harness/sensors/kpi_thresholds.py`) |
| `harness/review/sample.py` | decision-log review packets for weekly human review |
| `harness/review/judge.py` | LLM-as-judge scoring for diagnosis/action quality (`judge_verdict` + inferential `sensor_result`) |
| `harness/review/calibrate.py` | human-vs-judge agreement and trust gate (`trusted: true|false`) |
| `harness/review/pr_review.py` | selective PR advisory LLM review and failed-campaign log-quality reflection |
| `.github/CODEOWNERS`, `.github/pull_request_template.md` | risky-path ownership routing and risky-change PR checklist |
| `tests/harness_scenarios.py` | scripted runtime scenarios used by harness tests |
| `docs/harness/skills/` | step-by-step procedures (below) |

## E2E runtime map (what starts what)

- Target application entrypoint: `target/app/main.py` (uvicorn app).
- MI agent entrypoint: `src/mi_agent/main.py` (`python -m mi_agent.main`).
- API/dashboard surface: `src/mi_agent/api/server.py` (`/health/live`, `/agent/status`,
  `/agent/events`, `/agent/llm-trace`, `/agent/dashboard`).
- Fault campaign orchestrator: `scripts/e2e_fault_campaign.py` (uses
  `harness/scenarios/*.json` fixtures and writes campaign artifacts).
- Approved scenario checker: `harness/scenarios/check.py`.
- Event slicer + KPIs + invariant sensors: `harness/events/slice.py`,
  `harness/metrics/kpis.py`, `harness/sensors/runtime_invariants.py`.

## Runtime invariants (never break)

| Invariant | Enforced by |
| --- | --- |
| JEV selects every executed action | `tests/test_harness_architecture.py::test_jev_is_the_only_path_to_execution`, `harness/sensors/runtime_invariants.py::jev_selects_before_registration` |
| Security gate (step 9) before registration (10) and execution (11) | `tests/test_harness_architecture.py::test_security_gate_runs_before_registration_and_execution`, `harness/sensors/runtime_invariants.py::security_gate_before_registration` |
| Exactly one dynamic action per attempt; verifier always runs after it | `tests/test_harness_architecture.py::test_engine_executes_and_verifies_once_per_attempt_structurally`, `harness/sensors/runtime_invariants.py::one_action_per_attempt` |
| Retry loops back to step 5 with retained context | `harness/sensors/runtime_invariants.py::retry_resumes_at_step5` |
| A target repaired between attempts closes the incident without learning (pre-retry health check) | `harness/sensors/runtime_invariants.py::external_recovery_not_credited` |
| Success persistence only in steps 13–16, in order | `harness/sensors/runtime_invariants.py::persistence_only_after_verified_recovery` |
| Failure-path learning writes only on failed attempts | `harness/sensors/runtime_invariants.py::failure_learning_only_on_failed_attempts` |

## Boundaries

- `src/mi_agent` never imports `harness`. Harness code never imports `mi_agent` and never
  writes files (`tests/test_harness_architecture.py::test_harness_code_is_read_only`).
  Tests may import both.
- `state/` is written by the runtime only. Never hand-edit it in a change.
- Every mandatory loop step is called directly. Never use `getattr`/`hasattr` fallbacks
  that let a collaborator skip a step (e.g. the security gate).

## Risky changes (human approval required)

Defined in `harness/governance/README.md`: runtime control flow in the engine; the security
gate, operation allowlist, `src/mi_agent/tools/schemas.py`, `src/mi_agent/tools/pathing.py`;
JEV integration or thresholds; LLM prompts or parsing; contract major versions,
`workflow.step` enums, or sensor pass/fail rules; hand edits to runtime state; ignoring a
dependency advisory. Say in the PR description which item applies.

## Conventions

- Style: ruff (line length 100, rules `E F I UP B SIM RUF S`) and `mypy --strict` for both
  `src` and `harness`. The repo does not enforce `ruff format`; match the surrounding code.
- Security rules: fix findings. Suppress only inline (`# noqa: S…`) with a reason on the
  same line; `S101` is ignored in `tests/` only.
- Runtime data shapes are pydantic models (`src/mi_agent/workflow/models.py`). Event
  payloads may gain fields; renaming or removing a field needs the mapping doc
  (`docs/harness/HARNESS_RUNTIME_MAPPING.md`) and the dashboard reader updated in the same
  change.
- Tests: no network outside `@pytest.mark.live`; use `tmp_path`. Engine tests use
  duck-typed fakes that must implement the full tool interface, including
  `security_gate`. Prefer `tests/harness_scenarios.py` for end-to-end runtime behaviour.
- Contracts change in the same commit as the runtime change that needs them; the
  `workflow.step` grammar test fails otherwise.
- Dependencies: raise floors to fixed versions and verify per `harness/governance/README.md`
  § Dependency vulnerabilities.
- Commits: one logical change each, conventional prefix as in history
  (`fix(runtime): …`, `feat(harness): …`, `test(api): …`, `docs: …`, `ci(harness): …`).

## Real E2E prerequisites and startup

Real E2E is expected to run against the actual target app + MI process (not only unit tests).

Required runtime conditions:

- Docker path (recommended): `docker compose` daemon must be reachable.
- Host path (fallback): local process must be allowed to bind localhost ports.
- DNS/egress must reach the configured LLM endpoint and Pinecone host.
- Local Jev server must be up for action selection (`README.md` Jev section).

### Start target app (host mode)

```bash
cd target/app
TARGET_TEST_HOOKS=1 python -m uvicorn main:app --host 127.0.0.1 --port 8080
```

Readiness checks:

```bash
curl -fsS http://127.0.0.1:8080/health/live
curl -fsS http://127.0.0.1:8080/health/deep
```

### Start MI agent (host mode)

The `.env` file is compose-oriented (`/target/...`, `/state/...`). For host mode, override
state/file paths to writable local paths before running:

```bash
python -m mi_agent.main
```

Readiness checks:

```bash
curl -fsS http://127.0.0.1:8090/health/live
curl -fsS http://127.0.0.1:8090/agent/status
```

Monitoring active means `monitor.health` events continue in `/agent/events` and
`workflow_state` stays `healthy` when the target is healthy.

## E2E execution workflow

### Full fault campaign (supported faults only)

`scripts/e2e_fault_campaign.py` is the canonical runner for live fault injections.

```bash
PYTHONPATH=. python scripts/e2e_fault_campaign.py \
  --stage ci \
  --fixtures-dir harness/scenarios \
  --target-url http://127.0.0.1:8080 \
  --agent-url http://127.0.0.1:8090 \
  --events-log state/events.jsonl
```

Outputs:

- Under the gitignored results directory (per run): campaign summary JSON, incident slice JSONL files,
  approved-outcome check JSON files, and KPI JSON files.

### Inspecting evidence

```bash
curl -s http://127.0.0.1:8090/agent/events?limit=2000
curl -s http://127.0.0.1:8090/agent/llm-trace
tail -n 200 target/app/app.log
```

### Safe cleanup

- Host mode: stop target + MI processes explicitly.
- Compose mode: `docker compose down --remove-orphans` (and `-v` only when intentionally
  resetting backup history).

## Test lanes and constraints

- Unit/integration (offline): default `pytest -q` (`live` tests excluded by marker).
- Harness architecture/contract/sensor lane: `make harness`.
- Live LLM lane (external API required): `pytest -q -m live`.
- Live fault campaign lane (actual target + MI + faults):
  `scripts/e2e_fault_campaign.py` or `.github/workflows/live-e2e-fault-campaign.yml`.

Operational note: only the three fixture-backed faults are supported by the campaign
(`drop_widgets_table`, `delete_db`, `corrupt_db`). Scenarios outside these fixtures must be
reported as unsupported unless new approved fixtures are added.

## Procedures

| Task | Skill |
| --- | --- |
| Add or change a dynamic tool operation | `docs/harness/skills/add-dynamic-tool-operation.md` |
| Add or change a `workflow.step` | `docs/harness/skills/add-workflow-step.md` |
| Add or change a harness contract field | `docs/harness/skills/add-contract-field.md` |
| Review a change | `docs/harness/skills/review-checklist.md` |
