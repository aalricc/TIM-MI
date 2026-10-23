# Harness Governance

Where checks run, what needs human approval, how harness changes are promoted, and how
runtime-written artifacts are governed. Governance never relaxes a runtime invariant.

## Quality placement (keep quality left)

| Stage | Runs | Command | Status |
| --- | --- | --- | --- |
| **Pre-commit / every change** | lint, type checks (runtime + harness), unit tests, contract validation, structural architecture checks, scripted recovery regression | `.pre-commit-config.yaml` (same commands as `make check`) | wired; each clone runs `pre-commit install` once |
| | basic security scan | ruff `S` (flake8-bandit) rules, part of `ruff check .` | wired (pre-commit + CI) |
| **CI/CD after integration** | everything above again | `.github/workflows/ci.yml` (`ruff`, `mypy --strict src`, `mypy --strict harness`, `pytest -q`) | wired |
| | full E2E fault-injection recovery regression | `README.md` quick live recovery test | manual |
| | costlier inferential review (LLM code / architecture review) | — | gap |
| | required approval for risky changes | PR review (see below) | manual |
| **Continuous (outside change lifecycle)** | dependency / security drift scans | — | gap |
| | runtime KPI surveillance (recovery rate, TTR) | `python -m harness.metrics.kpis state/events.jsonl` | manual |
| | invariant surveillance on live logs | `python -m harness.sensors.runtime_invariants state/events.jsonl` | manual |
| | periodic decision-log sampling and review | `decision_trace` records via `harness.mappings.runtime.decision_traces` | manual |

## Changes that require explicit human approval

A change is **risky** and needs sign-off from a reviewer other than the author (or the
agent that drafted it) when it touches any of:

1. Runtime control flow in `src/mi_agent/agent/engine.py` (step order, retry, fast-path
   gates, persistence).
2. The security gate or operation allowlist (`ToolExecutor.security_gate`,
   `src/mi_agent/tools/schemas.py`, `src/mi_agent/tools/pathing.py`).
3. JEV integration or thresholds (`src/mi_agent/decision/`, `MI_JEV_*`, `MI_CLASSMEM_*`).
4. LLM prompts or output parsing (`src/mi_agent/llm/`).
5. A harness contract major version, a `workflow_step` enum, or any sensor's pass/fail rule.
6. Hand edits to runtime-written state (`state/skills.md`, `state/class_store.json`,
   `state/policy_store.json`, `state/tool_catalog.json`).

Non-risky (normal review): harness docs, new sensors that only add checks, KPI reporting,
test fixtures that do not weaken assertions.

## Runtime-written artifacts

| Artifact | Written by | Rule |
| --- | --- | --- |
| `state/tool_catalog.json` | step 13, success only | only the verified winning tool is persisted |
| `state/policy_store.json` | step 14 (+1) and failed attempts (−1, except `jev_request_error`) | no manual edits without approval |
| `state/skills.md` | step 15 | class sections are replaced in place per class; legacy blocks append; review before reuse in another environment |
| `state/class_store.json` | step 16 and failed outcomes | schema version guarded by `class_memory._SCHEMA_VERSION`; corrupt files are moved aside by the runtime |
| `state/events.jsonl` | `EventLog` | append-only; source of truth for sensors and KPIs |

Rollback: restore the previous file from backup or delete the learned entry, then record
the reason in `docs/WORKLOG.md`. A rollback of `skills.md` or `class_store.json` is a risky
change (item 6).

## Contract evolution

Additive, backward-compatible contract changes stay in v1. Breaking changes add a
`*.schema.v2.json` alongside v1; v1 is removed only when no consumer reads it. A runtime
change that alters the `workflow.step` grammar must update the contract in the same change
(enforced by `tests/test_harness_contracts.py`).

## Steering loop (S1–S6)

Run whenever an issue occurs more than once.

| Step | Actor | Action | Inputs / outputs |
| --- | --- | --- | --- |
| S1 Failure signals | MI / harness | Collect event log, decision log, metrics, failed regressions | `state/events.jsonl`, `decision_trace`, KPI JSON, failing sensors/tests |
| S2 Triage patterns | human | Group repeated failures, find root causes | group by `incident_signature.failure_type` / `class_id`, `decision_trace.reason`, sensor violations |
| S3 Draft a better control | LLM / coding agent | Propose a guide, sensor, test, or skill change | a PR that changes one guide/sensor/test/skill |
| S4 Validate | MI / CI | Run tests, contract, security, regression checks | `make check` locally, CI green |
| S5 Review & approve | human | Sign-off, mandatory for risky changes (list above) | PR approval |
| S6 Promote | CI/CD | Merge and deploy runtime and harness together | merge to `main` |

A repeated issue is closed only when a guide or sensor exists that would have prevented
or caught it.
