# Harness Guides (feedforward)

Guides steer MI, and the people and agents changing MI, before an action happens. Each
guide is computational (deterministic, enforced by tooling) or inferential (context an
LLM or human reads). Guides must stay inside the runtime loop in `README.md`: no guide may
add an execution stage, bypass JEV / security gate / verifier, or allow more than one
dynamic action per attempt.

Status: **in place** = exists and is used today; **gap** = defined here, not yet built.

Owner roles: **runtime maintainer** (owns `src/mi_agent`), **harness maintainer** (owns
`harness/`, `docs/harness/`, harness tests), **operator** (runs the stack, owns runbooks).

## Maintainability guides

| Guide | Type | Artifact | Owner | Status |
| --- | --- | --- | --- | --- |
| Typed runtime models | computational | `src/mi_agent/workflow/models.py`, `src/mi_agent/tools/schemas.py` (pydantic) | runtime maintainer | in place |
| Typed harness contracts | computational | `harness/contracts/*.schema.v1.json` | harness maintainer | in place |
| Lint / format / type policy | computational | `[tool.ruff]`, `[tool.mypy] strict` in `pyproject.toml` | runtime maintainer | in place |
| Dynamic-tool template | computational | `DynamicToolSpec` + per-operation params models; allowlist in `ToolExecutor.security_gate` | runtime maintainer | in place |
| Coding conventions for agents | inferential | `AGENTS.md` (setup, commands, invariants, boundaries, risky paths, conventions) | harness maintainer | in place |
| Contributor skills: add a dynamic tool operation / workflow step / contract field | inferential | `docs/harness/skills/add-dynamic-tool-operation.md`, `add-workflow-step.md`, `add-contract-field.md` | harness maintainer | in place |
| Review checklist | inferential | `docs/harness/skills/review-checklist.md` | harness maintainer | in place |

## Architecture guides

| Guide | Type | Artifact | Owner | Status |
| --- | --- | --- | --- | --- |
| Module boundaries (runtime ↔ harness, read-only harness) | computational | `docs/harness/HARNESS_ARCHITECTURE.md` § Runtime boundary | harness maintainer | in place |
| Runtime invariants (JEV, gate, one action, verifier, step-5 retry, success-only persistence) | computational | `docs/harness/HARNESS_ARCHITECTURE.md` § Invariants; `docs/harness/HARNESS_IMPLEMENTATION_WORKFLOW.md` § 9 | runtime maintainer | in place |
| Canonical `workflow.step` grammar | computational | `harness/contracts/workflow_step.schema.v1.json` | runtime + harness maintainer | in place |
| Architecture reference | inferential | `docs/ARCHITECTURE.md`, `docs/images/tim_mi_architecture.png` | runtime maintainer | in place |
| Logging / observability standard (`EventLog`, sanitisation, incident-scoped events) | inferential | `docs/harness/HARNESS_RUNTIME_MAPPING.md` § Runtime inputs | harness maintainer | in place |
| Threat model / security constraints | inferential | `docs/THREAT_MODEL.md` | runtime maintainer | in place |

## Behaviour guides

| Guide | Type | Artifact | Owner | Status |
| --- | --- | --- | --- | --- |
| Functional spec of recovery behaviour | inferential | `README.md` loop + runtime guarantees | runtime maintainer | in place |
| Fault-injection scenario fixtures | computational | target endpoints `POST /ops/fault/{delete_db,corrupt_db,drop_widgets_table}`; scripted scenarios in `tests/harness_scenarios.py` | harness maintainer | in place (scripted); gap (approved E2E fixture set) |
| Approved recovery outcomes | computational | expected outcome per fault scenario, recorded in `docs/harness/BASELINE.md` | operator | gap |
| Thresholds and their justification | inferential | `docs/THRESHOLD_JUSTIFICATION.md` | runtime maintainer | in place |
| Learned skills fed to HOW | inferential | `state/skills.md` (runtime-written, contract `skill_entry`) | runtime (auto) / operator review | in place |
| Runbooks and policy links | inferential | — | operator | gap |

## Changing a guide

Guide changes follow `harness/governance/README.md`: a guide that alters what the runtime
may do (allowlist, thresholds, invariants, prompts) is a risky change and needs explicit
human approval.
