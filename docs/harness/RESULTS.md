# Harness Results (Current Workflow)

Status: **pending comparison run.**

Baseline vs post-change KPIs for the same scenario set and configuration as
`docs/harness/BASELINE.md`. KPI definitions: `harness/metrics/README.md`.

## 1. Runs compared

| Field | Baseline | Post-change |
| --- | --- | --- |
| Git commit | TBD | TBD |
| Log slice file | TBD | TBD |
| Event log window | TBD | TBD |
| Configuration identical to baseline (yes / no + diff) | — | TBD |
| Change under evaluation (PR / guide / sensor / skill) | — | TBD |

## 2. Summary table

Delta = post-change − baseline. For TTR, attempts, request-error rate, and winning actions
per class, lower is better.

| KPI | Field | Baseline | Post-change | Delta |
| --- | --- | --- | --- | --- |
| Recovery rate | `recovery_rate` | TBD | TBD | TBD |
| TTR median (s) | `time_to_recover_seconds.median` | TBD | TBD | TBD |
| TTR p90 (s) | `time_to_recover_seconds.p90` | TBD | TBD | TBD |
| Attempts median | `attempts_per_recovered_incident.median` | TBD | TBD | TBD |
| Attempts p90 | `attempts_per_recovered_incident.p90` | TBD | TBD | TBD |
| JEV usable rate | `decision.usable_rate` | TBD | TBD | TBD |
| JEV request-error rate | `decision.jev_request_error_rate` | TBD | TBD | TBD |
| Selected with scores | `decision.selected_with_scores_rate` | TBD | TBD | TBD |
| Fast-path recovery share | `fast_path.fast_path_recovery_share` | TBD | TBD | TBD |
| Winning actions per class (max) | `winning_actions_per_class` | TBD | TBD | TBD |

## 3. Safety checks

Each must be `pass` in the post-change run (`python -m harness.sensors.runtime_invariants`)
and in CI (`tests/test_harness_architecture.py`).

| Invariant | Sensor / test | Result |
| --- | --- | --- |
| One dynamic action per attempt | `one_action_per_attempt` | TBD |
| JEV gate preserved | `jev_selects_before_registration`, `test_jev_is_the_only_path_to_execution` | TBD |
| Security gate preserved (step 9 before 10) | `security_gate_before_registration`, `test_security_gate_runs_before_registration_and_execution` | TBD |
| Verifier gate preserved | `verification_follows_execution` | TBD |
| Retry loops back to step 5 | `retry_resumes_at_step5` | TBD |
| Success persistence only in steps 13–16 | `persistence_only_after_verified_recovery` | TBD |
| Failure-path writes only on failed attempts | `failure_learning_only_on_failed_attempts` | TBD |

## 4. Observations

Repeated failure patterns (group by `failure_type`, `class_id`, `decision_trace.reason`): TBD

## 5. Conclusion

Promote / do not promote, with reason (steering loop S5): TBD
