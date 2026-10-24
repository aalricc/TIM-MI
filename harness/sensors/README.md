# Harness Sensors (feedback)

Sensors observe after MI (or a change to MI) acts, so the system can self-correct.
Sensors are observational: they read code, test results, the event log, and state files,
and never mutate runtime control flow.

Run stages (placement rationale: `harness/governance/README.md`):

- **pre_commit** — fast computational checks on every change (`.pre-commit-config.yaml`,
  same commands as `make check`).
- **ci** — everything in pre_commit again, plus costlier checks after integration.
- **continuous** — outside the change lifecycle, against live runtime artifacts.

Owner roles as in `harness/guides/README.md`. Status: **wired** = runs automatically at
the listed stage; **manual** = command exists, run by a person; **gap** = not built.

## Maintainability sensors

| Sensor | Type | Stage | Implementation | Owner | Status |
| --- | --- | --- | --- | --- | --- |
| Lint | computational | pre_commit, ci | `ruff check .` | runtime maintainer | wired (CI, `make check`) |
| Type check (runtime) | computational | pre_commit, ci | `mypy --strict src` | runtime maintainer | wired |
| Type check (harness) | computational | pre_commit, ci | `mypy --strict harness` | harness maintainer | wired |
| Unit tests | computational | pre_commit, ci | `pytest -q` (excludes `live`) | runtime maintainer | wired |
| Contract validity + runtime grammar equality | computational | pre_commit, ci | `tests/test_harness_contracts.py` | harness maintainer | wired |
| Contract shape vs runtime output | computational | pre_commit, ci | `tests/test_harness_mappings.py` | harness maintainer | wired |
| Basic security scan (static) | computational | pre_commit, ci | ruff `S` (flake8-bandit) rules in `ruff check .`; suppressions are inline with a reason, plus `S101` in `tests/**` | runtime maintainer | wired |
| Dependency vulnerability scan | computational | ci, continuous | `pip-audit` over `.[dev,jev,rag]` (everything the image ships + dev tools): CI job `audit` in `.github/workflows/ci.yml`; weekly fresh-resolution run in `.github/workflows/drift.yml` (opens/updates a tracking issue on failure); local `make audit` | runtime maintainer | wired |
| LLM code review (duplication, over-engineering) | inferential | ci (selective) | — | harness maintainer | gap |
| PR review | human | ci | GitHub pull request review | change author's reviewer | manual |

## Architecture sensors

| Sensor | Type | Stage | Implementation | Owner | Status |
| --- | --- | --- | --- | --- | --- |
| Runtime ↔ harness import boundary | computational | pre_commit, ci | `test_runtime_does_not_import_harness`, `test_harness_does_not_import_runtime` | harness maintainer | wired |
| Harness is read-only | computational | pre_commit, ci | `test_harness_code_is_read_only` | harness maintainer | wired |
| Security gate before registration (engine) and execution (executor) | computational | pre_commit, ci | `test_security_gate_runs_before_registration_and_execution` | runtime maintainer | wired |
| JEV is the only path to execution | computational | pre_commit, ci | `test_jev_is_the_only_path_to_execution` | runtime maintainer | wired |
| One execution + one verification call site | computational | pre_commit, ci | `test_engine_executes_and_verifies_once_per_attempt_structurally` | runtime maintainer | wired |
| README loop (16 steps, step-5 retry) ↔ mapping doc drift | computational | pre_commit, ci | `test_readme_loop_has_sixteen_steps_and_step5_retry_rule`, `test_mapping_doc_covers_every_readme_step` | harness maintainer | wired |
| Operation allowlist consistency (gate = LLM HOW schema = LLM prompt = test copies = `docs/ARCHITECTURE.md`) | computational | pre_commit, ci | `test_operation_allowlist_is_consistent` | runtime maintainer | wired |
| Guide reference integrity (every path and `path::Symbol` in `AGENTS.md` and `docs/harness/skills/` exists) | computational | pre_commit, ci | `test_agent_guides_reference_existing_code` | harness maintainer | wired |
| `jev_selects_before_registration` | computational | ci, continuous | `harness/sensors/runtime_invariants.py` | harness maintainer | wired (ci, via `tests/test_harness_sensors.py`; continuous, via `harness-watch`) |
| `security_gate_before_registration` | computational | ci, continuous | same | harness maintainer | as above |
| `one_action_per_attempt` | computational | ci, continuous | same | harness maintainer | as above |
| `verification_follows_execution` | computational | ci, continuous | same | harness maintainer | as above |
| `persistence_only_after_verified_recovery` | computational | ci, continuous | same | harness maintainer | as above |
| `failure_learning_only_on_failed_attempts` | computational | ci, continuous | same | harness maintainer | as above |
| `external_recovery_not_credited` (incident closed by the pre-retry health check is not followed by any execution or persistence) | computational | ci, continuous | same | harness maintainer | as above |
| `retry_resumes_at_step5` (retry-loop check) | computational | ci, continuous | same | harness maintainer | as above |
| Runtime budget (latency, token, rate) | computational | continuous | — (TTR covered by KPIs; no token/rate budget yet) | runtime maintainer | gap |
| Architecture review agent / log-quality reflection after failed run | inferential | ci (selective) | — | harness maintainer | gap |
| Architecture-drift report review | human | ci | review of failures from the rows above | runtime maintainer | manual |

Continuous run against live state: the compose `harness-watch` service runs
`python -m harness.watch /state/events.jsonl` every `HARNESS_WATCH_INTERVAL_SECONDS`
(default 300) over the last 24 h and appends one `sensor_result` per sensor to
`results/harness/sensor_results.jsonl` (all records of one run share `ts`). The MI
dashboard's Surveillance panel shows the latest run (`GET /agent/harness`). By hand:

```bash
python -m harness.watch state/events.jsonl            # JSONL to stdout; exit 1 on any failure
python -m harness.sensors.runtime_invariants state/events.jsonl   # invariants only, whole log
```

Run sensors only on log windows written by the current runtime. Logs from before the
`security.gate` step existed fail `security_gate_before_registration` by construction;
slice the campaign window first (`docs/harness/BASELINE.md` § 4).

## Behaviour sensors

| Sensor | Type | Stage | Implementation | Owner | Status |
| --- | --- | --- | --- | --- | --- |
| Scripted recovery regression (full loop, unusable decisions, failure + unhealthy ping, fast path fail/fallback, cold start) | computational | pre_commit, ci | `tests/harness_scenarios.py` driven by `tests/test_harness_*.py`; `tests/test_agent_workflow.py`, `tests/test_class_memory_engine.py` | runtime maintainer | wired |
| Approved outcome checker (fixture + log slice) | computational | ci, continuous | `python -m harness.scenarios.check [--stage ci|continuous] harness/scenarios/<fault>.json <log-slice>.jsonl`; uses `harness.events.slice`, `decision_trace`, KPIs, and invariant sensors | operator | wired (ci via tests); manual (continuous) |
| E2E fault injection (inject → recover → verify via `/health/deep`) | computational | ci | `python scripts/e2e_fault_campaign.py --stage ci` in `.github/workflows/live-e2e-fault-campaign.yml` (`workflow_dispatch` on self-hosted runner) | operator | wired (`workflow_dispatch`; passed 15/15, `docs/harness/BASELINE.md`) |
| Live LLM integration | computational | ci (opt-in) | `pytest -m live` in `.github/workflows/live-e2e-fault-campaign.yml` | runtime maintainer | wired (`workflow_dispatch`) |
| Recovery KPIs (recovery rate, TTR, attempts, decision telemetry) | computational | continuous | `python -m harness.metrics.kpis state/events.jsonl` | operator | manual |
| `kpi_thresholds` (recovery rate ≥ 0.9, p90 TTR ≤ 60 s, JEV request-error rate ≤ 0.1 over 24 h; `skipped` when no KPI is defined) | computational | continuous | `harness/sensors/kpi_thresholds.py`, thresholds in `harness/metrics/thresholds.json`, run by `harness/watch.py` | operator | wired (`harness-watch`) |
| LLM-as-judge on diagnosis quality and action choice | inferential | continuous (sampled) | — (input: `decision_trace` + `incident_signature` records) | harness maintainer | gap |
| Decision-log sampling and review | human | continuous | sample `decision_trace` records weekly | operator | manual |
| Manual testing / approve risky changes | human | ci | `harness/governance/README.md` | reviewer | manual |

## Primary runtime signal sources

`workflow.step`, `decision.selected` / `decision.unusable`, `class_memory.decision`,
`class_memory.fast_path_skipped`, `planning.how`, `dynamic_tool.executed`,
`verification.result`, `incident.started` / `incident.recovered` /
`incident.recovery_failed` / `incident.closed`, `rag.call` / `rag.error` / `rag.empty` /
`rag.skipped`, `agent.loop_error`.

## Sensor output

Every harness sensor emits a `sensor_result` v1 record (`harness/contracts`). A `fail`
result always carries at least one violation with `incident_id`, `attempt`, and `detail`.
