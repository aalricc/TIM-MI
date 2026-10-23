# MI Harness Architecture

## Purpose

The harness is the control plane around MI's runtime loop (`README.md`, steps 1–16;
`docs/images/tim_mi_architecture.png`). It steers MI before actions (guides) and
observes outcomes after actions (sensors), without entering the runtime's critical path
or changing its execution semantics.

## Model

Two control surfaces:

- **Guides (feedforward)** — contracts, skills, runbooks, policy that shape what MI and
  its developers do before an action. Catalogue: `harness/guides/README.md`.
- **Sensors (feedback)** — event log, decision log, metrics, and checks that observe
  results so MI and its developers can self-correct. Catalogue: `harness/sensors/README.md`.

Three lanes, each with guides and sensors:

| Lane | Question | Example guide | Example sensor |
| --- | --- | --- | --- |
| Maintainability | Is the code healthy? | typed contracts, ruff/mypy config | `ruff check`, `mypy --strict`, contract tests |
| Architecture fitness | Are boundaries and invariants intact? | module boundaries, runtime invariants | `tests/test_harness_architecture.py`, `harness/sensors/runtime_invariants.py` |
| Behaviour | Does recovery actually work? | fault-injection fixtures, recovery spec | E2E fault injection, recovery KPIs |

Each guide/sensor is computational (deterministic), inferential (LLM-judged), or human.

## Layers and code map

| Layer | Location | Role |
| --- | --- | --- |
| Contracts | `harness/contracts/*.schema.v1.json`, `harness/contracts/__init__.py` | Versioned JSON Schemas + validator for harness records |
| Mappings | `harness/mappings/runtime.py` | Deterministic read model: event log / state files → records |
| Sensors | `harness/sensors/runtime_invariants.py`, `tests/test_harness_*.py` | Invariant checks emitting `sensor_result` records |
| Metrics | `harness/metrics/kpis.py` | Reference KPI computation (spec in `harness/metrics/README.md`) |
| Governance | `harness/governance/README.md` | Where checks run, promotion, approvals, steering loop |
| Docs | `docs/harness/` | Mapping, baseline and results templates, this file |

Data flow (one direction only):

```text
runtime (src/mi_agent) ──writes──▶ state/events.jsonl, state/*.json, state/skills.md
                                            │
                                            ▼  read-only
harness/mappings ──▶ contract records ──▶ harness/sensors, harness/metrics ──▶ humans (S1–S6)
                                                                                │
                         guides/sensors/tests updated through CI ◀──────────────┘
```

## Runtime boundary

- Runtime execution stays in `src/mi_agent`; it never imports `harness`.
- Harness code never imports `mi_agent` and never writes files (enforced by
  `tests/test_harness_architecture.py`). Tests may drive the runtime to produce outputs.
- Learned runtime artifacts stay in `state/`.
- Harness work does not change the step order in `README.md`.

## Core records

`incident_signature`, `problem_class`, `class_action_stats`, `skill_entry`,
`sensor_result`, `decision_trace`, `workflow_step` — see `harness/contracts/README.md`.

## Invariants and where they are enforced

| Invariant | Structural check | Event-log sensor |
| --- | --- | --- |
| JEV mandatory for action selection | `test_jev_is_the_only_path_to_execution` | `jev_selects_before_registration` |
| Security gate before registration and execution | `test_security_gate_runs_before_registration_and_execution` | `security_gate_before_registration` |
| Exactly one dynamic action per attempt | `test_engine_executes_and_verifies_once_per_attempt_structurally` | `one_action_per_attempt` |
| Verifier independent and always run after execution | same structural test | `verification_follows_execution` |
| Retry loops back to step 5 | — | `retry_resumes_at_step5` |
| Success persistence only in steps 13–16, in order | — | `persistence_only_after_verified_recovery` |
| Failure-path learning writes only on failed attempts, policy before class memory | — | `failure_learning_only_on_failed_attempts` |
| Fast path does not bypass JEV / gate / verifier | covered by the rows above (fast path uses the same `_select_candidate_with_jev` → `_execute_selected_action` path) | path-mode-aware `jev_selects_before_registration` |
