# MI Harness

Harness control plane around the runtime loop in `README.md` (steps 1–16): guides
(feedforward) and sensors (feedback) across the maintainability, architecture-fitness, and
behaviour lanes. Architecture: `docs/harness/HARNESS_ARCHITECTURE.md`.

Scope:

- Versioned data contracts for harness records.
- Deterministic, read-only mappings from runtime events/state files to those records.
- Guide, sensor, metric, and governance definitions with run stages and owners.

Non-goals:

- No runtime loop redesign or new recovery stage.
- No bypass of JEV, security gate, or verifier.
- No change to one-action-per-attempt semantics.

Directory map:

- `contracts/` — `*.schema.v1.json` + `validate_record` / `contract_errors`
- `mappings/` — `runtime.py`: event log, `class_store.json`, `skills.md` → records
- `sensors/` — sensor catalogue; `runtime_invariants.py` event-log invariant sensors
- `metrics/` — KPI specs; `kpis.py` reference computation
- `guides/` — feedforward guide catalogue
- `governance/` — quality placement, approvals, promotion, steering loop

Commands:

```bash
make harness                                              # contract/mapping/sensor/KPI tests + mypy
python -m harness.sensors.runtime_invariants state/events.jsonl   # exit 1 on violation
python -m harness.metrics.kpis state/events.jsonl                 # KPI JSON
```

Harness code imports nothing from `mi_agent` and writes no files.
