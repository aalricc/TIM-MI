# MI Harness Scaffolding Workflow (Diagram-Aligned)

This document is the build checklist for harness scaffolding, aligned to `docs/images/tim_mi_architecture.png` and the runtime loop in `README.md`.

## 1) Scope and non-goals

### Scope
- Scaffold harness control-plane assets around the existing MI runtime.
- Keep runtime and harness boundaries explicit:
  - Runtime execution remains in `src/mi_agent/*`.
  - Harness scaffolding remains in `harness/*` and `docs/harness/*`.
- Define feedforward guides, feedback sensors, and promotion checks.

### Non-goals
- No runtime control-flow redesign.
- No bypass of JEV, security gate, verifier, or one-action-per-attempt semantics.
- No new recovery stage inserted into the runtime loop.

## 2) Runtime source of truth (must stay unchanged)

Use this exact runtime sequence (steps 1–16):

1. Monitor health + logs.
2. Collect runtime evidence.
3. RAG symptom grounding.
4. LLM call #1: diagnose WHY.
5. Class-memory lookup.
6. RAG repair guidance (when fast path is not eligible).
7. LLM call #2: HOW candidate actions (when fast path is not eligible).
8. JEV (System One): evaluate candidates, choose one usable action.
9. Security-only safety gate (schema/path/sandbox checks before execution).
10. Register dynamic tool (selected tool, active incident only).
11. Execute one dynamic action.
12. Verify recovery (independent check).
13. Persist winning tool to `state/tool_catalog.json`.
14. Update RL policy rewards in `state/policy_store.json`.
15. Append learned recovery skill to `state/skills.md`.
16. Update class memory in `state/class_store.json`.

Retry rule:
- If not recovered at step 12, retain context and loop back to step 5 (no hard cap).
- Before each retry, re-check health (step 1); if already healthy, close the incident
  without persisting anything (amended 2026-10-23, approved).

## 3) Harness model from the diagram

Harness is two control surfaces around the runtime:

- **Guides (feedforward):** steer MI before actions.
- **Sensors (feedback):** observe outcomes after actions and drive correction.

Harness exists in three lanes:

1. **Maintainability harness** (internal code quality).
2. **Architecture fitness harness** (boundaries and invariants).
3. **Behavior harness** (does recovery actually work).

Human steering rule:
- When issues repeat, update guides and sensors so recurrence likelihood drops.

## 4) Scaffolding deliverables (directory-by-directory)

## `harness/contracts/`
Deliver versioned deterministic schemas for:
- `incident_signature`
- `problem_class`
- `class_action_stats`
- `skill_entry`
- `sensor_result`
- `decision_trace`
- `workflow_step`

Requirements:
- Schema fields match runtime payloads and event taxonomy.
- Decision schema supports both usable and unusable JEV outcomes.
- `workflow_step` schema reflects canonical runtime step grammar.

## `harness/guides/`
Document feedforward controls in three groups:

- Maintainability guides:
  - Typed contracts, lint/format policy, dynamic-tool templates.
- Architecture guides:
  - Module boundaries, invariants, logging/observability standards.
- Behavior guides:
  - Functional recovery specs, scenario fixtures, runbook policy links.

## `harness/sensors/`
Document feedback sensors in three groups:

- Maintainability sensors:
  - Lint/type/unit/contract checks, dependency/security scans, review checks.
- Architecture sensors:
  - Boundary/fitness tests, runtime-budget and retry-loop checks, architecture-drift checks.
- Behavior sensors:
  - Fault-injection E2E checks, recovery regressions, diagnosis/action-quality review.

## `harness/metrics/`
Define KPI specs from runtime artifacts:
- Recovery rate.
- Time to recover.
- Attempts per incident.
- Decision usability/selection telemetry quality.

## `harness/governance/`
Define promotion and approval policy:
- What runs pre-commit.
- What runs in CI/CD.
- What runs continuously outside change lifecycle.
- What requires explicit human approval.

## 5) Quality placement (exactly as diagram intent)

### Pre-commit / on every change
Run fast computational sensors only:
- lint
- type checks
- unit tests
- contract validation
- basic security scan

### CI/CD after integration
Run:
- all pre-commit checks again
- full recovery regression
- costlier inferential review checks
- required approvals for risky changes

### Continuous (outside change lifecycle)
Run:
- dependency/security drift scans
- runtime KPI surveillance (recovery rate/time to recover)
- periodic decision-log sampling and review

## 6) Steering loop (S1–S6)

Use this exact control loop for harness improvement:

1. **Failure signals:** collect event log, decision log, metrics, failed regressions.
2. **Triage patterns:** group repeated failures and identify root causes.
3. **Draft better control:** propose guide/sensor/test/skill change.
4. **Validate:** run test, contract, security, and regression checks.
5. **Review & approve:** human sign-off, especially for risky changes.
6. **Promote:** deploy approved harness updates through CI/CD.

## 7) Implementation phases (for actual build work)

### Phase A — Contract alignment
- Align schemas with runtime payloads and step taxonomy.
- Add tests that assert shape compatibility against current runtime outputs.

### Phase B — Mapping alignment
- Define deterministic mappings from runtime events/payloads to harness records.
- Keep mappings read-model only (no control-flow side effects).

### Phase C — Guide and sensor scaffolding
- Finalize guide/sensor docs per maintainability, architecture, behavior lanes.
- Add check ownership and required run stages (pre-commit/CI/continuous).

### Phase D — Baseline and result templates
- Prepare `docs/harness/BASELINE.md` and `docs/harness/RESULTS.md` for campaign reporting.
- Keep terminology tied to current runtime workflow and KPIs.

## 8) Definition of done for scaffolding

Scaffolding is ready when all are true:
- Runtime steps (1–16) and retry rule match `README.md`.
- Harness docs reflect the guides/sensors split and three-lane model.
- Contract docs and tests match emitted runtime payloads.
- Quality placement is explicit for pre-commit, CI/CD, and continuous runs.
- Steering loop is documented and actionable.

## 9) Mandatory invariants checklist

Before starting harness implementation, confirm:
- JEV remains mandatory in runtime action selection.
- Security gate remains mandatory before execution.
- Exactly one dynamic action executes per attempt.
- Verifier remains independent.
- Retry loops back to step 5 when not recovered (after the pre-retry health check).
- A target repaired outside MI between attempts is never credited to MI.
- Success persistence remains steps 13–16 only.
