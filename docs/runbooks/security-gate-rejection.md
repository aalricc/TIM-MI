# Runbook: security-gate rejection

The security gate (README step 9,
`src/mi_agent/tools/executor.py::ToolExecutor.security_gate`) rejected an action that JEV
had selected. Nothing was executed.

## Symptoms

- Event log: `workflow.step` `security.gate` = `rejected` with `operation`, `error_type`,
  `error`; then `persist.policy_store` = `failure`, `attempt.result` = `failed`.
- `incident.recovery_failed` with `error` starting `security_gate_rejected:`.
- KPI `decision.gate_rejected` > 0 (`python -m harness.metrics.kpis state/events.jsonl`).

List rejections (repository root, virtualenv active):

```bash
python - <<'EOF'
from pathlib import Path
from harness.mappings.runtime import load_event_log, workflow_steps
for s in workflow_steps(load_event_log(Path("state/events.jsonl"))):
    if s["step"] == "security.gate" and s["status"] == "rejected":
        print(s["incident_id"], s["attempt"], s["path_mode"], s["operation"], s["error_type"], s["error"])
EOF
```

## What MI does on its own

A rejection is a failed attempt: the action is excluded for the rest of the incident, the
reason is fed to the next HOW call, the tool gets reward −1 in `state/policy_store.json`,
class memory records a failure for that action, and the loop retries from step 5. A
repeatedly rejected learned template loses fast-path eligibility. No action is needed for a
single rejection.

## Read the error

| `error` contains | Meaning | Response |
| --- | --- | --- |
| `operation not allowed by security gate` | an LLM proposed an operation outside the allowlist and JEV selected it | JEV should not select it; if it recurs, review JEV behaviour (risky change area) |
| `path escapes sandbox` / `path escapes app root` | `replace_file` path outside `/target` (source) or `/target/app` (destination) | expected protection; if a learned fast-path template keeps producing it, review class memory |
| a pydantic validation message (`error_type` `ValidationError`) | parameters do not match the operation's model in `src/mi_agent/tools/schemas.py` | usually an LLM parameter mistake; check whether `src/mi_agent/agent/engine.py::RecoveryAgent._normalize_candidate` should map the alias |

## Never

Do not widen the allowlist, the path roots, or a parameter model to make a rejected action
pass. Changing the gate is a risky change (`harness/governance/README.md`) and follows
`docs/harness/skills/add-dynamic-tool-operation.md`.

## Escalate

Rejections on 3 or more attempts in one incident, or in several incidents for the same
class: follow `docs/runbooks/stuck-incident.md` for the incident and start the steering
loop (S1–S6) for the cause.
