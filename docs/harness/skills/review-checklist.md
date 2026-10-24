# Skill: review checklist

Use for every pull request, whether written by a person or an agent. Steering-loop step
S5 in `harness/governance/README.md`.

## 1. Scope and risk

- [ ] One logical change; the commit message uses a conventional prefix.
- [ ] If any risky-change category in `harness/governance/README.md` applies, the PR names
      it and a reviewer other than the author approves.
- [ ] No hand edits under `state/`.

## 2. Runtime invariants

- [ ] No new stage in the README loop; step order in `README.md` unchanged.
- [ ] JEV still selects every executed action; no path reaches
      `src/mi_agent/agent/engine.py::RecoveryAgent._execute_selected_action` without it.
- [ ] Security gate still runs before registration and again inside
      `src/mi_agent/tools/executor.py::ToolExecutor.execute_dynamic_tool`.
- [ ] Still at most one dynamic execution per attempt, always followed by verification.
- [ ] Failed attempts still loop back to step 5; success persistence still steps 13–16.
- [ ] No `getattr`/`hasattr` fallbacks that let a collaborator skip a mandatory step.

## 3. Contracts and harness

- [ ] Event payload or step changes come with contract, `README_STEP`, and mapping-doc
      updates in the same commit (`docs/harness/skills/add-workflow-step.md`).
- [ ] Harness code still imports nothing from `mi_agent` and writes no files.
- [ ] New or changed sensors have a passing case on every scenario and a failing case.

## 4. Tests

- [ ] New behaviour has a test that fails without the change.
- [ ] Tests were not weakened (assertions removed, expectations loosened) without a stated
      reason.
- [ ] No network access outside `@pytest.mark.live`.

## 5. Security and dependencies

- [ ] ruff `S` findings fixed, or suppressed inline with a reason.
- [ ] Dependency floor changes verified per `harness/governance/README.md`
      § Dependency vulnerabilities; `make audit` clean.
- [ ] No secrets in code, tests, fixtures, or logs.

## 6. Docs

- [ ] Guide/sensor catalogue status updated if a gap was closed
      (`harness/guides/README.md`, `harness/sensors/README.md`).
- [ ] References in `AGENTS.md` and `docs/harness/skills/` still resolve
      (`tests/test_harness_architecture.py::test_agent_guides_reference_existing_code`).

## 7. Checks

- [ ] `make check` passes locally; CI (`test`, `audit`) is green.
