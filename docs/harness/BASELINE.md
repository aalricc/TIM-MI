# Harness Baseline (Current Workflow)

Status: **pending collection.**

Pre-change baseline for the runtime loop in `README.md` (steps 1–16). KPI definitions:
`harness/metrics/README.md`. Fill every `TBD`; leave a field `n/a` only with a reason.

## 1. Run identity

| Field | Value |
| --- | --- |
| Date (UTC) | TBD |
| Git commit (`git rev-parse HEAD`) | TBD |
| Operator | TBD |
| Event log window (first / last `ts`) | TBD |
| Log slice file | TBD (e.g. `results/baseline-YYYYMMDD.jsonl`) |

## 2. Configuration (must match the comparison run)

| Setting | Value |
| --- | --- |
| `MI_LLM_MODEL` | TBD |
| `MI_JEV_MODEL` / `MI_JEV_USABLE_THRESHOLD` | TBD |
| `MI_CLASSMEM_JOIN_THRESHOLD` / `MARGIN_DELTA` / `TAU_CONF` / `N_MIN` / `COOLDOWN_SECONDS` | TBD |
| `MI_RAG_FETCH_DEPTH` / `MI_RAG_QUERY_REWRITE` / `PINECONE_NAMESPACE` | TBD |
| Class-store state at start (`empty` or snapshot file) | TBD |
| `state/skills.md` at start (`empty` or snapshot file) | TBD |

## 3. Scenario set

Each run is one fault injected against a healthy target via `POST /ops/fault/{fault}`
(target started with `TARGET_TEST_HOOKS=1`). `scripts/e2e_fault_campaign.py` runs the
faults round-robin (S-01, S-02, S-03, S-01, ...) and keeps MI's memory across runs, so
later repetitions measure learning (fast path). Before each run it waits for a healthy
target; if a previous run left it unhealthy it calls `POST /ops/reset` (recorded as
`reset_before`), then waits until MI has no open incident. The harness never acts on the
target while MI handles an incident. `state/` starts empty for every CI campaign.

A run passes when the incident is verifiably recovered within the fixture's budgets and
every runtime invariant holds; fast-path use is a reported KPI, not a pass condition.

| ID | Fault | Repetitions | Expected recovered outcome (approved) |
| --- | --- | --- | --- |
| S-01 | `drop_widgets_table` | 5 | `run_sql` recreating `widgets`, or `request_backup_restore` |
| S-02 | `delete_db` | 5 | `request_backup_restore` (or `replace_file`) |
| S-03 | `corrupt_db` | 5 | `request_backup_restore` (or `replace_file`) |
| S-04 | TBD (optional) | TBD | TBD |
| S-05 | TBD (optional) | TBD | TBD |

## 4. Collection

The CI campaign (`.github/workflows/live-e2e-fault-campaign.yml`) produces everything
below per run under `results/<run-id>/`: per-run slice, check and KPI files, and
`campaign.json` (per-run `status`, `recovered_via` = `fast_path`/`full_loop`, `attempts`,
`ttr_seconds`, `reset_before`, plus a per-fault `summary`) and `campaign.kpis.json` (KPIs
over all runs). Locally, with the stack up:

```bash
PYTHONPATH=. python scripts/e2e_fault_campaign.py --stage ci --repetitions 5
```

The same records by hand from any event-log window:

```bash
python -m harness.events.slice \
  --since "<iso-or-epoch>" \
  --until "<iso-or-epoch>" \
  state/events.jsonl > <log-slice>.jsonl

python -m harness.scenarios.check --stage continuous harness/scenarios/drop_widgets_table.json <log-slice>.jsonl \
  > <log-slice>.drop_widgets_table.check.json
python -m harness.scenarios.check --stage continuous harness/scenarios/delete_db.json <log-slice>.jsonl \
  > <log-slice>.delete_db.check.json
python -m harness.scenarios.check --stage continuous harness/scenarios/corrupt_db.json <log-slice>.jsonl \
  > <log-slice>.corrupt_db.check.json

python -m harness.metrics.kpis <log-slice>.jsonl > <log-slice>.kpis.json
python -m harness.sensors.runtime_invariants <log-slice>.jsonl > <log-slice>.sensors.json
```

## 5. KPIs

| KPI | Field in `kpis.json` | Value |
| --- | --- | --- |
| Incidents started / recovered / self-resolved / open | `incidents` | TBD |
| Recovery rate | `recovery_rate` | TBD |
| TTR median / p90 / max (s) | `time_to_recover_seconds` | TBD |
| Attempts per recovered incident median / p90 / max | `attempts_per_recovered_incident` | TBD |
| Attempt outcomes (recovered / retry / failed) | `attempt_outcomes` | TBD |
| JEV usable rate | `decision.usable_rate` | TBD |
| JEV request-error rate | `decision.jev_request_error_rate` | TBD |
| Top unusable reasons | `decision.unusable_reasons` | TBD |
| Selected decisions with scores | `decision.selected_with_scores_rate` | TBD |
| Security-gate rejections | `decision.gate_rejected` | TBD |
| Fast-path recovery share | `fast_path.fast_path_recovery_share` | TBD |
| Distinct winning actions per class | `winning_actions_per_class` | TBD |

Per-scenario breakdown (repeat the KPI rows per scenario ID if scenarios were run in
separate windows): TBD

## 6. Invariant sensors

| Sensor | Status | Checked | Violations |
| --- | --- | --- | --- |
| `jev_selects_before_registration` | TBD | TBD | TBD |
| `security_gate_before_registration` | TBD | TBD | TBD |
| `one_action_per_attempt` | TBD | TBD | TBD |
| `verification_follows_execution` | TBD | TBD | TBD |
| `persistence_only_after_verified_recovery` | TBD | TBD | TBD |
| `failure_learning_only_on_failed_attempts` | TBD | TBD | TBD |
| `retry_resumes_at_step5` | TBD | TBD | TBD |
| `external_recovery_not_credited` | TBD | TBD | TBD |

## 7. Notes

- Keep environment and configuration constant between baseline and comparison runs.
- Use canonical runtime events only (`workflow.step`, `incident.*`, `decision.*`).
- Security-gate rejections are failed attempts (`decision.gate_rejected`); list the
  rejected actions and reasons here: TBD
- Record any `agent.loop_error` events (unexpected runtime exceptions) here: TBD
