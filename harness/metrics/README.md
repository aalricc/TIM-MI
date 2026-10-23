# Harness Metrics

KPI specifications computed only from canonical runtime events in `state/events.jsonl`
(never from ad-hoc logs). Reference implementation: `harness/metrics/kpis.py`
(`python -m harness.metrics.kpis state/events.jsonl`); tests: `tests/test_harness_kpis.py`.

## Cohort

The **cohort** is every incident with an `incident.started` event inside the log window.
Events for incidents whose start is outside the window are ignored. When an incident has
several `incident.recovered` events, the first one counts.

| Bucket | Definition |
| --- | --- |
| recovered | cohort incident with `incident.recovered` |
| self_resolved | cohort incident with `incident.closed` but no `incident.recovered` (monitor saw healthy before MI recovered it) |
| open | cohort incident with neither (still active, or the process stopped) |

## KPIs

### 1. Recovery rate
`recovery_rate = recovered / started` over the cohort. `null` when `started = 0`.

### 2. Time to recover (TTR)
Per recovered incident: `incident.recovered.ts − incident.started.ts` (seconds). Reported
as `n`, `median`, `p90` (nearest rank: the ⌈0.9·n⌉-th smallest value), and `max`.
TTR includes every attempt, including unusable-decision attempts and post-recovery pings.

### 3. Attempts per incident
Per recovered incident: `incident.recovered.attempts`, the runtime's attempt counter at
recovery. Counts executed attempts **and** attempts that ended in an unusable decision
(no execution). Same summary statistics as TTR. `attempt_outcomes` counts
`workflow.step:attempt.result` by status (`recovered`, `retry`, `failed`).

### 4. Decision usability and selection telemetry quality
From `workflow.step:jev.result` (one per JEV arbitration, fast path and full loop):

| Field | Definition |
| --- | --- |
| `jev_results` | count of `jev.result` with status `usable` or `unusable` |
| `usable_rate` | `usable / jev_results`. JEV verdict only: a usable verdict naming a non-existent candidate (`selected_candidate_not_found`) still counts as usable here |
| `unusable_reasons` | `jev.result.reason` counts for `unusable` |
| `jev_request_error_rate` | unusable with reason prefix `jev_request_error` / `jev_results` (transport failures; these are not penalised in the RL policy) |
| `selected` | count of `decision.selected` |
| `gate_rejected` | count of `workflow.step:security.gate` with status `rejected` (JEV-selected actions the security gate blocked; each is a failed attempt) |
| `selected_with_scores_rate` | share of `decision.selected` whose `decision.usable_probability` and `decision.risk_score` are both numbers |

Pre-JEV rejections (`all_candidates_previously_failed`) emit no `jev.result` and are
visible in `decision_trace` records and `attempt_outcomes` instead.

### Supporting metrics

| Metric | Definition |
| --- | --- |
| `fast_path_recovery_share` | among recovered cohort incidents whose `incident.recovered` carries `source` (class memory enabled), the share with `source = class_memory`; `null` if none carry `source` |
| `winning_actions_per_class` | per `class_id`, number of distinct `action_key` values that won a recovery; lower is more stable |

## Interpretation notes

- Compare campaigns only with identical configuration and scenario sets
  (`docs/harness/BASELINE.md`).
- `state/events.jsonl` is append-only across runtime versions; slice it to the campaign
  window before computing KPIs.
