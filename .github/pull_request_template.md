## Summary

- What changed:
- Why this change is needed:

## Risky-Change Checklist (Governance)

If this PR touches a risky area from `harness/governance/README.md`, complete all items.

- [ ] This PR touches risky paths.
- [ ] Risky governance item(s) touched (check all that apply):
  - [ ] 1. Runtime control flow (`src/mi_agent/agent/engine.py`)
  - [ ] 2. Security gate / allowlist (`security_gate`, `tools/schemas.py`, `tools/pathing.py`)
  - [ ] 3. JEV / class-memory thresholds and controls
  - [ ] 4. LLM prompts or output parsing
  - [ ] 5. Harness contract major version / `workflow_step` enum / sensor pass-fail rule
  - [ ] 6. Hand edits to runtime-written state
  - [ ] 7. Judge rubric grading criteria or thresholds
- [ ] Evidence links are provided:
  - [ ] `make check` evidence:
  - [ ] Sensor/KPI/campaign artifact links (if relevant):
  - [ ] Additional test evidence (if relevant):

## Review and Approval

- [ ] A reviewer approved this PR per governance.
- [ ] If this is a risky change, approval rationale is stated.

> Temporary solo-owner exception (CP14/D6): while runtime maintainer, harness maintainer,
> and operator are all `@aalricc`, self-approval is allowed. Remove this exception after
> assigning at least one additional maintainer role owner.

## Steering Loop Link

- [ ] If this addresses a repeated issue, update or reference the steering loop action in
      `harness/governance/README.md` (S1–S6).

