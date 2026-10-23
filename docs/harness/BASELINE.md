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

Each scenario is one fault injected against a healthy target via
`POST /ops/fault/{fault}`, with the next fault injected only after `incident.closed`.

| ID | Fault | Repetitions | Expected recovered outcome (approved) |
| --- | --- | --- | --- |
| S-01 | `drop_widgets_table` | TBD | TBD (e.g. `run_sql` recreating `widgets`) |
| S-02 | `delete_db` | TBD | TBD (e.g. `request_backup_restore`) |
| S-03 | `corrupt_db` | TBD | TBD |
| S-04 | TBD (optional) | TBD | TBD |
| S-05 | TBD (optional) | TBD | TBD |

## 4. Collection

```bash
# slice the campaign window out of state/events.jsonl into the log slice file, then:
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

## 7. Notes

- Keep environment and configuration constant between baseline and comparison runs.
- Use canonical runtime events only (`workflow.step`, `incident.*`, `decision.*`).
- Security-gate rejections are failed attempts (`decision.gate_rejected`); list the
  rejected actions and reasons here: TBD
- Record any `agent.loop_error` events (unexpected runtime exceptions) here: TBD
