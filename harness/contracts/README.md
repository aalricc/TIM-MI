# Harness Contracts

Versioned JSON Schema (draft 2020-12) contracts for harness records. Field-level
derivations: `docs/harness/HARNESS_RUNTIME_MAPPING.md`.

| Contract | One record per | Built from |
| --- | --- | --- |
| `workflow_step.schema.v1.json` | `workflow.step` event | event payload + `ts` + `readme_step` |
| `decision_trace.schema.v1.json` | JEV arbitration outcome (`selected` / `gate_rejected` / `unusable` / `fast_path_fallback`) | `planning.how` + `decision.selected`, `security.gate=rejected` + `decision.selected`, `decision.unusable`, `class_memory.fast_path_skipped` |
| `incident_signature.schema.v1.json` | incident | `incident.started`, `monitor.unhealthy_detected`, first `diagnosis.why`, first `class_memory.decision` |
| `problem_class.schema.v1.json` | class in `state/class_store.json` | `classes.<class_id>` |
| `class_action_stats.schema.v1.json` | class action | `classes.<class_id>.actions.<action_key>` |
| `skill_entry.schema.v1.json` | skill in `state/skills.md` | class sections and legacy frontmatter blocks |
| `scenario_fixture.schema.v1.json` | approved fault scenario fixture | `harness/scenarios/*.json` |
| `review_verdict.schema.v1.json` | one human verdict for a sampled decision packet | operator-reviewed packets under `results/reviews/` |
| `judge_verdict.schema.v1.json` | one LLM judge verdict for a sampled decision packet | `python -m harness.review.judge` output |
| `sensor_result.schema.v1.json` | sensor run | harness sensors (never the runtime) |

Validation:

```python
from harness.contracts import contract_errors, validate_record

validate_record(record, "decision_trace")      # raises ValueError on violation
contract_errors(record, "workflow_step")       # list[str], empty when valid
```

Rules:

- Contracts describe what the runtime emits. LLM-originated fields (candidate `name`,
  `operation`) are free strings; the operation allowlist is enforced by the runtime
  security gate, not by contracts.
- `workflow_step` enums must equal the runtime's literal set exactly
  (`tests/test_harness_contracts.py` extracts it from `engine.py`/`executor.py`).
  A runtime change that adds a step or status fails that test until the contract is
  updated in the same change.
- Every contract is checked against real runtime output in `tests/test_harness_mappings.py`.
- Additive, backward-compatible changes stay in the same major version.
- Breaking changes create a new major file (`*.schema.v2.json`) and keep v1 until all
  consumers move.
