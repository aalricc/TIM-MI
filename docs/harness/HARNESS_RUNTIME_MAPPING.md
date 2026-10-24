# Harness Runtime Mapping (Current Workflow)

How harness records are derived from what the runtime already emits. Runtime source of
truth: the 16-step loop in `README.md`, implemented in `src/mi_agent/agent/engine.py`.

Reference implementation: `harness/mappings/runtime.py` (read-model only — it reads the
event log and state files, never imports `mi_agent`, never writes). Mappings pass runtime
values through unchanged so contract validation surfaces payload drift.

## Runtime inputs

| Input | Writer | Format |
| --- | --- | --- |
| `state/events.jsonl` | `EventLog.publish` | one `{"ts", "event_type", "payload"}` per line; payload passed through `sanitize_payload` (secrets redacted, strings > 1200 chars truncated, lists > 60 items end with a `"<truncated:N items>"` marker, depth > 6 becomes `"<truncated:depth>"`) |
| `state/class_store.json` | `ClassMemory._save_state` | `{"schema_version", "updated_ts", "classes": {class_id: record}}` |
| `state/skills.md` | `ClassMemory.upsert_skills_section` (class memory on) / `RecoveryAgent._append_skill_entry` (class memory off) | Markdown |
| `state/policy_store.json` | `RecoveryAgent._update_policy` | `{"updated_at", "actions": {tool_name: {attempts, reward_total}}}` (no harness contract; read by KPIs only via events) |
| `state/tool_catalog.json` | `ToolExecutor.persist_dynamic_tool` | `{tool_name: DynamicToolSpec}` (no harness contract) |

## Canonical `workflow.step` grammar

Payload keys: `incident_id`, `attempt`, `path_mode`, `step`, `status`, plus step-specific
metadata. Allowed values are pinned by `harness/contracts/workflow_step.schema.v1.json`
and checked for exact equality against the runtime source by
`tests/test_harness_contracts.py` (AST extraction, including f-string and conditional
values).

- `path_mode`: `monitor`, `class_memory`, `fast_path`, `full_loop`
- `status`: `detected`, `disabled`, `empty`, `error`, `failed`, `failure`, `fallback`,
  `healthy`, `ok`, `recovered`, `rejected`, `retry`, `skipped`, `started`, `unhealthy`,
  `unusable`, `usable`

## README step → runtime step → harness record

`readme_step` is added to every `workflow_step` record from this table
(`README_STEP` in `harness/mappings/runtime.py`).

| README step | Runtime `workflow.step` (statuses) | Other runtime events | Harness record |
| --- | --- | --- | --- |
| 1 | `monitor.unhealthy_detected` (`detected`); `monitor.healthy_before_retry` (`healthy`, retries only) | `monitor.health`, `incident.started`; `incident.closed` (`resolution` = `healthy_before_retry`) | `incident_signature` (`started_at`, `monitor_detail`) |
| 2 | `diagnostics.collected` (`ok`) | — | `workflow_step` |
| 3 | `rag.symptom` (`ok`/`empty`/`skipped`/`error`) | `rag.call` (`phase=symptom`), `rag.empty`/`rag.skipped`/`rag.error` | `workflow_step` |
| 4 | `llm.why` (`ok`) | `diagnosis.why` | `incident_signature` (`failure_type`, `hypotheses`, `evidence`) |
| 5 | `context.reused` (`ok`, retries only), `class_memory.lookup` (`ok`/`disabled`), `path.selected` (`ok`) | `class_memory.decision` | `incident_signature` (`class_id`, `class_match`) |
| 6 | `rag.repair` (`ok`/`empty`/`skipped`/`error`) | `rag.call` (`phase=repair`) | `workflow_step` |
| 7 | `llm.how` (`ok`) | — (proposal published later in `planning.how`) | `decision_trace.candidates` |
| 8 | `jev.requested` (`started`), `jev.result` (`usable`/`unusable`), `path.fallback` (`fallback`) | `decision.selected`, `decision.unusable`, `class_memory.fast_path_skipped` | `decision_trace` |
| 9 | `security.gate` (`ok`/`rejected`) | on `rejected`: `incident.recovery_failed` | `workflow_step`; `decision_trace` (`gate_rejected`) |
| 10 | `dynamic_tool.registered` (`ok`) | `planning.how` | `decision_trace.selected_candidate` |
| 11 | `dynamic_tool.executed` (`ok`/`error`) | `dynamic_tool.executed` (`operation`, `ok`, `error`, executor output nested under `result`) | `workflow_step` |
| 12 | `verification.result` (`ok`/`failed`) | `verification.result` | `workflow_step` |
| 13 | `persist.tool_catalog` (`ok`) | — | `workflow_step` (governance audit) |
| 14 | `persist.policy_store` (`ok` = +1 on success; `failure` = −1 on a failed attempt) | — | `workflow_step` (governance audit) |
| 15 | `persist.skills` (`ok`/`skipped`) | — | `skill_entry` (parsed from `state/skills.md`) |
| 16 | `persist.class_memory` (`ok`/`skipped`; `failure` = failed outcome recorded) | — | `problem_class`, `class_action_stats` (from `state/class_store.json`) |

Markers outside the numbered loop (`readme_step = null`):

- `post_recovery.ping` (`healthy`/`unhealthy`): monitor re-check after steps 13–16. If
  `unhealthy`, the attempt ends `retry` even though persistence already ran.
- `attempt.result` (`recovered`/`retry`/`failed`): attempt outcome. `retry`/`failed` →
  the next iteration first re-checks health: still unhealthy → re-enters at step 5 with
  `context.reused` (`retry_from_step5`); already healthy → `monitor.healthy_before_retry`
  (recorded under the last attempt number) and the incident closes.

## Record derivations

### `workflow_step`
Payload copied verbatim, plus `schema_version`, event `ts`, `readme_step`.

### `decision_trace` (one per arbitration outcome, log order)
Context is tracked per incident from preceding `workflow.step` events: `attempt` (latest
step), `source` (`path.selected` path mode, overridden by `jev.requested.source`), and
`evaluated_count` (`jev.requested.candidate_count`).

| Trigger event | `outcome` | `candidates` / `candidates_origin` | `selected_candidate` | `decision` |
| --- | --- | --- | --- | --- |
| `planning.how` (preceded by `decision.selected`) | `selected` | `primary` + `alternatives` / `how_proposal` | name+operation from `decision.selected`; `executed_candidate`, `executed_tool_name` from `planning.how` | `decision.selected.decision` |
| `workflow.step:security.gate` = `rejected` (preceded by `decision.selected`) | `gate_rejected` | `[]` / `not_emitted` (`evaluated_count` from `jev.requested`) | name + `executed_candidate` from `decision.selected`, `operation` from the gate step, `executed_tool_name` = `null`; `reason` = gate `error_type` | `decision.selected.decision` |
| `decision.unusable` | `unusable` | `candidate_actions` / `jev_evaluated` | `null` | `decision.unusable.decision` |
| `class_memory.fast_path_skipped` | `fast_path_fallback` | `[]` / `not_emitted` (`evaluated_count` = payload `candidate_count`) | `null` | payload `decision` |

### `incident_signature` (one per incident, first-appearance order)
`started_at` = `incident.started.ts`; `monitor_detail` = `monitor.unhealthy_detected.detail`;
`failure_type`/`hypotheses`/`evidence` from the first `diagnosis.why`; `class_id` and
`class_match` (`s1`, `s2`, `margin`, `new_class`, `fast_path_eligible`,
`first_failing_gate_reason`) from the first `class_memory.decision`.

### `problem_class` / `class_action_stats`
One `problem_class` per `classes.<id>` (centroid summarised as `centroid_dim`). One
`class_action_stats` per `classes.<id>.actions.<action_key>`; `successes = alpha − 1`,
`failures = beta − 1` (Beta(1,1) prior, +1 per outcome), so `attempts = successes + failures`.

### `skill_entry`
`class_section` from `<!-- class:<id> begin/end -->` blocks; `legacy_block` from
`---\nname: recovery-…` frontmatter blocks.

## Runtime behaviours the harness must account for

Observed facts about the current runtime, recorded so harness consumers do not misread
the data.

1. **Security gate (step 9).** The engine calls `ToolExecutor.security_gate` and emits
   `security.gate` before registration (step 10), matching `README.md`. The executor
   re-runs the same gate inside `execute_dynamic_tool` so no caller can execute an ungated
   spec. A rejection (`SecurityGateError`, or pydantic `ValidationError` for bad params) is
   a failed attempt (decision D1 = b): `security.gate` = `rejected` (`error_type`,
   `error`), `persist.policy_store` = `failure` (−1 on `base_tool_name(spec)`, the key a
   success would use), `persist.class_memory` = `failure` when class memory is on,
   `incident.recovery_failed`, `attempt.result` = `failed`. The action key joins
   `failed_action_keys`, the rejection reason (`security_gate_rejected:<ErrorType>: …`)
   feeds the next HOW call, and the loop retries from step 5. Nothing is registered or
   executed. Any other exception from the gate is a bug and still propagates
   (`agent.loop_error`).
2. **Persistence events on both outcomes.** Success persistence is steps 13–16 with status
   `ok`/`skipped`. A failed attempt (verification failed, unusable decision, or gate
   rejection) also
   writes learning state and emits `persist.policy_store` = `failure` (`reward` −1) and,
   when class memory recorded the failed action, `persist.class_memory` = `failure`, in
   that order and before `attempt.result` = `failed`. Security-gate rejections follow the
   same pattern (item 1). `jev_request_error` (transport
   failure) writes nothing and emits no persist step. A rejected fast path that falls
   back to the full loop writes nothing.
3. **Skills on first success.** Step 15 renders the class section with the pending
   success (`ClassMemory.render_skills_section(pending=…)`), so a new class gets its skill
   on its first recovery and the section matches what step 16 then persists (same
   timestamp). `persist.skills` = `skipped` now only occurs if the class record is missing.
4. **`dynamic_tool.executed` payload.** Shape: `incident_id`, `tool_name`, `operation`,
   `ok`, `error`, `result` (raw executor output), plus `source`/`action_key` when class
   memory is enabled. Event logs written before this change spread the executor output at
   the top level, where its `source` key (a backup path) collided with the arbitration
   source; the dashboard still reads the legacy `action.operation` form for such logs.
5. **`selected_candidate_not_found`.** JEV can return `usable=true` naming a candidate that
   does not exist; the runtime emits `decision.unusable` carrying that usable decision.
6. **Fast-path fallback stays in the same attempt.** A rejected fast path emits
   `path.fallback`, then `path.selected` (`full_loop`) under the same `attempt`, so one
   attempt can contain two `jev.requested` steps but still at most one execution.
7. **`decision.unusable` has no `class_id`** and `decision.selected` has no `source`; the
   harness takes them from context, never invents them.
8. **Pre-retry health check.** Each retry starts with `monitor_health`. If the target is
   healthy, the engine emits `monitor.healthy_before_retry`, publishes `incident.closed`
   with `resolution` = `healthy_before_retry` (no `incident.recovered`), and persists
   nothing. KPIs count such incidents as `self_resolved`.
9. **Step 5 also writes `state/class_store.json`** (`ClassMemory.assign_class` updates the
   class centroid and may create a class). Its event is `class_memory.lookup`.
10. **Backup restores and action keys.** Diagnostics carry the target's backup catalog
    (`backups`, from `GET /ops/backups`); the HOW request gets it as `backup_catalog`.
    `request_backup_restore` has one class-memory action key whatever `backup_id` it names,
    so `incident.recovered.action_key` and class stats do not split by backup. Within an
    incident a failed restore excludes only that backup. A fast-path restore binds
    `backup_id` to the newest catalog backup created before the incident was detected; if
    there is none, or that restore already failed in this incident, `path.selected`
    (`full_loop`) carries `reason` = `fast_path_params_unbound` or
    `fast_path_action_already_failed`.

## Alignment constraints

- Do not add runtime loop stages for harness purposes.
- Do not bypass JEV, security gate, or verifier.
- Harness parsers accept both usable and unusable decision outcomes.
- Timeline and history views stay incident-scoped.
