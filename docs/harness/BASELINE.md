# Harness Baseline (Current Workflow)

Status: **collected 2026-10-24** (CI run `40111431118`, campaign `fault-campaign-20261024T162525Z`, overall `pass`).

Pre-change baseline for the runtime loop in `README.md` (steps 1–16). KPI definitions:
`harness/metrics/README.md`. Fill every `TBD`; leave a field `n/a` only with a reason.

## 1. Run identity

| Field | Value |
| --- | --- |
| Date (UTC) | 2026-10-24 |
| Git commit (`git rev-parse HEAD`) | `e36c3fc850c8af3a48e20174e84456b36801922b` |
| Operator | aalricc (`workflow_dispatch`, self-hosted macOS arm64 runner) |
| Event log window (first / last `ts`) | 2026-10-24T16:24:57Z / 2026-10-24T16:28:42Z |
| Log slice file | workflow artifact `live-e2e-fault-campaign-results`: `state/events.jsonl` (509 events) and `results/fault-campaign-20261024T162525Z/` |

## 2. Configuration (must match the comparison run)

| Setting | Value |
| --- | --- |
| `MI_LLM_MODEL` | `gpt-5.3-codex` (Azure OpenAI Responses endpoint, `MI_LLM_AUTH_MODE=api_key`) |
| `MI_JEV_MODEL` / `MI_JEV_USABLE_THRESHOLD` | `jev-latest` / `0.65` |
| `MI_CLASSMEM_JOIN_THRESHOLD` / `MARGIN_DELTA` / `TAU_CONF` / `N_MIN` / `COOLDOWN_SECONDS` | `0.82` / `0.08` / `0.80` / `3` / `30` |
| `MI_RAG_FETCH_DEPTH` / `MI_RAG_QUERY_REWRITE` / `PINECONE_NAMESPACE` | `10` / `true` / `sqlite-docs` |
| Class-store state at start (`empty` or snapshot file) | `empty` |
| `state/skills.md` at start (`empty` or snapshot file) | `empty` |

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
| S-04 | not run (optional) | n/a | n/a |
| S-05 | not run (optional) | n/a | n/a |

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
| Incidents started / recovered / self-resolved / open | `incidents` | 15 / 15 / 0 / 0 |
| Recovery rate | `recovery_rate` | 1.0 |
| TTR median / p90 / max (s) | `time_to_recover_seconds` | 12.63 / 18.62 / 28.62 |
| Attempts per recovered incident median / p90 / max | `attempts_per_recovered_incident` | 1 / 1 / 1 |
| Attempt outcomes (recovered / retry / failed) | `attempt_outcomes` | 15 / 0 / 0 |
| JEV usable rate | `decision.usable_rate` | 1.0 (15 / 15) |
| JEV request-error rate | `decision.jev_request_error_rate` | 0.0 |
| Top unusable reasons | `decision.unusable_reasons` | none |
| Selected decisions with scores | `decision.selected_with_scores_rate` | 1.0 |
| Security-gate rejections | `decision.gate_rejected` | 0 |
| Fast-path recovery share | `fast_path.fast_path_recovery_share` | 0.467 (7 / 15) |
| Distinct winning actions per class | `winning_actions_per_class` | 1 class (`caee531893b3`), 2 actions |

Per-scenario breakdown (one window, round-robin; no run needed `reset_before`):

| ID | Pass | Full loop / fast path | TTR median (s) | Winning action per repetition (r1…r5) |
| --- | --- | --- | --- | --- |
| S-01 `drop_widgets_table` | 5 / 5 | 2 / 3 | 12.39 | `run_sql` ×2, then `request_backup_restore` ×3 (fast path) |
| S-02 `delete_db` | 5 / 5 | 3 / 2 | 12.63 | `request_backup_restore` ×5 |
| S-03 `corrupt_db` | 5 / 5 | 3 / 2 | 13.88 | `request_backup_restore` ×5 |

TTR median by path: full loop 13.91 s (n=8), fast path 6.41 s (n=7). First fast-path use was
run 9 (S-01 r3), after five verified restores in the class.

Finding (class granularity): all three faults joined one incident class (`caee531893b3`,
join similarity 0.86–0.96, threshold 0.82). Class memory therefore learned restore from
S-02/S-03 and replayed it on S-01 r3–r5. This is not a regression: `DROP TABLE` already
lost every row, so the `run_sql` recreate in r1–r2 left an empty table, while a restore
keeps rows up to the last backup (≤ 10 s old). No workload wrote widgets during this
campaign, so data loss was not measured; there is no `rows_lost` KPI yet.

## 6. Invariant sensors

| Sensor | Status | Checked | Violations |
| --- | --- | --- | --- |
| `jev_selects_before_registration` | pass | 15 | 0 |
| `security_gate_before_registration` | pass | 15 | 0 |
| `one_action_per_attempt` | pass | 15 | 0 |
| `verification_follows_execution` | pass | 15 | 0 |
| `persistence_only_after_verified_recovery` | pass | 15 | 0 |
| `failure_learning_only_on_failed_attempts` | pass | 0 | 0 |
| `retry_resumes_at_step5` | pass | 0 | 0 |
| `external_recovery_not_credited` | pass | 0 | 0 |

## 7. Notes

- Keep environment and configuration constant between baseline and comparison runs.
- Use canonical runtime events only (`workflow.step`, `incident.*`, `decision.*`).
- Security-gate rejections are failed attempts (`decision.gate_rejected`); list the
  rejected actions and reasons here: none (0 rejections).
- Record any `agent.loop_error` events (unexpected runtime exceptions) here: 1 —
  `LLMError: LLM request failed: ` (empty message) at 16:28:11Z during S-01 r4
  (`inc-bb552b8168`), about 21 s after `rag.symptom`, i.e. in the WHY LLM call. The next monitor tick
  resumed the incident, took the fast path and recovered on attempt 1; this is why that
  run's TTR (28.62 s) is the max. The empty message suggests a timeout exception with no
  text; the error type is not recorded.
- `failure_learning_only_on_failed_attempts`, `retry_resumes_at_step5` and
  `external_recovery_not_credited` checked 0 events: every incident recovered on attempt 1
  and none recovered externally, so this baseline does not exercise those paths.
