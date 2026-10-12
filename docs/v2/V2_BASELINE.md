# MI v2 baseline audit (Phase 0)

Date: 2026-10-07

## 1) Module map (current runtime)

- `src/mi_agent/runtime.py`: composition root (`build_agent`).
- `src/mi_agent/agent/`: monitor loop, diagnosis, planning, selection, execution orchestration.
- `src/mi_agent/tools/`: primitive schemas, allowlist executor, path guard, snapshot + rollback.
- `src/mi_agent/supervisor/`: incident lock, attempt/time caps, backoff, last-resort restore trigger.
- `src/mi_agent/verifier/`: post-action health gate.
- `src/mi_agent/policy/`: tabular action values + lessons JSON store.
- `src/mi_agent/skills/`: skill distillation, matching, library persistence and stats.
- `src/mi_agent/llm/`: OpenAI Responses adapter + deterministic fake client for tests.
- `src/mi_agent/api/`: FastAPI state/policy/skills/incidents/SSE/dashboard routes.

## 2) Real control loop (as implemented)

`MonitorLoop` pings DB every `monitor_interval_seconds`; on failure it calls `MIAgent._recover_loop`.

Recovery sequence:
1. Acquire supervisor incident lock.
2. On each gated attempt: gather logs + probes.
3. Try exact-signal skill match first; if none, ask LLM to match skill.
4. If no skill, call LLM diagnose, then LLM plan proposal.
5. Build candidate first steps from plans, filter invalid/failed repeats, epsilon-greedy select.
6. Execute selected primitive/skill via allowlisted tool executor.
7. Verifier decides success/failure.
8. On failure: rollback snapshot (if present), penalize action, continue.
9. On success: reward action(s), persist policy update, try to distill/register skill.
10. If caps are reached: supervisor runs last-resort restore, verifier re-checks, send alert.

## 3) Primitive registry baseline

Current registry source: `src/mi_agent/tools/executor.py` (`ToolExecutor._specs`).

| Primitive | Params model | mutating | reversible |
|---|---|---:|---:|
| `ping_db` | `EmptyParams` | no | no |
| `read_app_log` | `ReadAppLogParams(lines: int=100, 1..2000)` | no | no |
| `list_directory` | `ListDirectoryParams(path: str='.')` | no | no |
| `stat_path` | `StatPathParams(path: str)` | no | no |
| `read_app_file` | `ReadAppFileParams(path: str)` | no | no |
| `check_schema` | `EmptyParams` | no | no |
| `create_directory` | `CreateDirectoryParams(path: str)` | yes | no |
| `create_empty_sqlite_db` | `CreateEmptySQLiteDBParams(path: str='data/app.db')` | yes | yes |
| `apply_schema` | `ApplySchemaParams(db_path='data/app.db', schema_path='schema.sql')` | yes | yes |
| `set_permissions` | `SetPermissionsParams(path: str, mode ∈ {0o600,0o640,0o660,0o700,0o750})` | yes | yes |
| `restart_service` | `RestartServiceParams(service_name='target-app')` | yes | no |

### Parameter auto-fill defaults

`ToolExecutor.with_default_parameters()` auto-fills:

- `list_directory.path` → DB directory relative to sandbox root.
- `stat_path.path` → DB file relative to sandbox root.
- `read_app_file.path` → schema file relative to app root.
- `create_directory.path` → DB directory relative to sandbox root.
- `set_permissions.path` + `set_permissions.mode` → DB file + `0o660`.

## 4) Learned-state storage and memory locations

- Policy store: `MISettings.policy_store_path` (default `./state/policy_store.json`).
  - Keying: `failures.<canonical_failure_type>.actions.<action_key>`.
  - Also stores `lessons` by failure type.
- Skill store: `MISettings.skill_store_path` (default `./state/skills.json`).
  - Stores `SkillRecord` list with steps, trigger fingerprints, stats, active/version metadata.
- Failed-action memory: in-process only (`AgentActionExecutor._failed_attempts: dict[action_key -> state_fingerprint]`), not persisted.

## 5) Seeded state status (`state/*.json`)

Current repository state is seeded (non-empty):

- `state/policy_store.json` contains `db_file_missing` action values and a lesson.
- `state/skills.json` contains a pre-existing learned skill record.

This violates empty-start learning-integrity expectations and is registered in `KNOWN_VIOLATIONS.md`.

## 6) Failure-type branching status

- Canonicalizer (`src/mi_agent/failure_types.py`) maps raw text to canonical types using pattern branches (`db_file_missing`, `db_corrupt`, `disk_full`, `service_down`) before fallback slugging.
- Verifier (`src/mi_agent/verifier/verifier.py`) branches on canonical failure type via `_inferred_failure_check`, with special handling for `db_file_missing`.

Verifier branching is registered as a known violation for Phase 1 cleanup.

## 7) `apply_schema` idempotence (established)

Established by `tests/unit/test_phase0_baseline_behaviors.py::test_apply_schema_current_behavior_is_not_idempotent_with_plain_create`.

Observed behavior:

- On an empty DB with current `schema.sql`, first `apply_schema` succeeds.
- A second `apply_schema` call raises `sqlite3.OperationalError` (`table ... already exists`).

So current behavior is **not idempotent** for plain `CREATE TABLE` schema statements.

## 8) `restart_service` behavior (established)

Established by:

- `tests/unit/test_phase0_baseline_behaviors.py::test_restart_service_without_hook_is_a_noop_success_result`
- `tests/unit/test_phase0_baseline_behaviors.py::test_restart_service_with_hook_calls_hook_once`

Observed behavior:

- Without hook: returns success payload only; no side effects in runtime code.
- With hook: forwards exactly one call with provided `service_name` and returns success payload.

## 9) Last-resort restore trigger conditions

Last-resort restore is triggered when `Supervisor.gate_attempt()` returns `cap_reached=True`:

- attempts cap: `incident.attempts >= max_attempts_per_incident`, or
- wall-clock cap: elapsed seconds `>= max_incident_seconds`.

At cap, agent calls `Supervisor.run_last_resort()` (latest backup restore), then verifies and alerts.

## 10) Current test coverage snapshot

Collected with `pytest --collect-only -q`:

- Total tests: **66**.
- Integrity marker tests: **6** (`pytest --collect-only -q -m integrity`).
- Phase 0 marker tests: **7** (`pytest --collect-only -q -m phase0`).
- Live-network smoke test exists but is opt-in (`MI_RUN_LIVE_LLM_TEST=1`), skipped by default.

Per-file test counts are currently:

- `tests/contract/test_integrity_enforcement.py`: 6
- `tests/contract/test_v2_gate_contract.py`: 4
- `tests/integration/test_agent_recovery.py`: 10
- `tests/integration/test_live_llm_smoke.py`: 1
- `tests/unit/test_agent_executor_and_sanitization.py`: 3
- `tests/unit/test_event_bus.py`: 1
- `tests/unit/test_failure_type_normalization.py`: 5
- `tests/unit/test_llm_client.py`: 2
- `tests/unit/test_monitor.py`: 2
- `tests/unit/test_phase0_baseline_behaviors.py`: 3
- `tests/unit/test_policy_store.py`: 4
- `tests/unit/test_selector.py`: 3
- `tests/unit/test_skills.py`: 9
- `tests/unit/test_state_machine.py`: 2
- `tests/unit/test_supervisor.py`: 5
- `tests/unit/test_tools_guardrails.py`: 6

