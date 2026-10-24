# Skill: add or change a `workflow.step`

`workflow.step` events are the canonical timeline every harness sensor, KPI, and the
dashboard reads. Adding a step or status changes the `workflow_step` contract enums, which
is a **risky change** (`harness/governance/README.md`). It must never add a new stage to
the README loop; a step describes something the loop already does.

## Steps

1. **Emit it.** Call `src/mi_agent/agent/engine.py::RecoveryAgent._publish_workflow_step`
   with `step=`, `status=`, and `path_mode=` given as string literals, conditional
   expressions of literals, or variables assigned from literals. The contract test resolves
   these statically from the source and fails on anything it cannot resolve. Extra
   keyword arguments become payload fields.
2. **Contract enums.** Add new `step`, `status`, or `path_mode` values to
   `harness/contracts/workflow_step.schema.v1.json` in the same commit. Removing a value is
   a breaking change (old logs stop validating): follow the breaking path in
   `docs/harness/skills/add-contract-field.md` and point the grammar test at the v2 file.
   `tests/test_harness_contracts.py::test_workflow_step_schema_matches_runtime_grammar_exactly`
   requires the enums to equal the runtime's values exactly.
3. **README step.** Map the new step in `harness/mappings/runtime.py::README_STEP` to its
   README loop step (1–16), or `None` for markers outside the numbered loop.
   `tests/test_harness_contracts.py::test_readme_step_mapping_covers_every_step_and_loop_position`
   enforces coverage.
4. **Mapping doc.** Add the step and its statuses to the README-step table in
   `docs/harness/HARNESS_RUNTIME_MAPPING.md`, and describe any new runtime behaviour in its
   "Runtime behaviours" section.
5. **Sensors.** If the step takes part in an invariant (gate, JEV, execution,
   verification, persistence, retry), update the matching sensor in
   `harness/sensors/runtime_invariants.py` and add a failing-case test in
   `tests/test_harness_sensors.py`. Sensors must pass on every scenario in
   `tests/harness_scenarios.py`.
6. **KPIs.** If the step changes how incidents, attempts, or decisions are counted,
   update `harness/metrics/kpis.py`, `harness/metrics/README.md`, and the KPI tables in
   `docs/harness/BASELINE.md` and `docs/harness/RESULTS.md`.
7. **Ordered-sequence tests.** Update expected sequences, e.g.
   `tests/test_harness_mappings.py::test_workflow_steps_follow_readme_step_order`.
8. **Dashboard.** `dashboard/index.html` renders steps generically; change it only if the
   step needs special display.

## Done when

```bash
make check
python -m harness.sensors.runtime_invariants <log from a run of the changed runtime>
```

Sensors applied to logs written before the change may fail by construction (for example,
logs without `security.gate`); slice to post-change windows.
