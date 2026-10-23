# Runbook: incident not recovering

The runtime retries without a cap, so "stuck" is an operator threshold: escalate when an
incident has **3 or more failed attempts** or has been open **more than 5 minutes**, or when
MI itself is erroring.

## Check

```bash
curl -s http://localhost:8090/agent/status        # incident.attempts, incident.last_tool_error
docker compose logs --tail 100 mi-agent
```

Last attempts and their reasons (repository root, virtualenv active):

```bash
python - <<'EOF'
from pathlib import Path
from harness.mappings.runtime import load_event_log, workflow_steps
WATCH = {"jev.result", "security.gate", "verification.result", "attempt.result"}
for s in [s for s in workflow_steps(load_event_log(Path("state/events.jsonl"))) if s["step"] in WATCH][-20:]:
    detail = s.get("reason") or s.get("failure_type") or s.get("error") or ""
    print(s["incident_id"], s["attempt"], s["step"], s["status"], detail)
EOF
```

## Diagnose

| Pattern | Cause | Response |
| --- | --- | --- |
| `jev.result` `unusable`, reason `jev_request_error:…`, repeated | JEV server unreachable | start or fix `jev serve` (`README.md` § Jev local server); check `MI_JEV_BASE_URL`. These attempts do not penalise the policy |
| `agent.loop_error` events with `error_type` `LLMError` (`docker compose logs`, `/agent/events`) | LLM endpoint or credentials failing; iterations abort before an attempt | fix `OPENAI_API_KEY` / `MI_LLM_*`; MI resumes on its own |
| `rag.error` steps | Pinecone failing | recovery continues without RAG context; fix `PINECONE_*` if quality suffers |
| `verification.result` `failed` with the same `failure_type` every attempt | MI's actions do not fix this fault | manual takeover with the fault runbook (`docs/runbooks/drop_widgets_table.md`, `docs/runbooks/delete_db.md`, `docs/runbooks/corrupt_db.md`) |
| `attempt.result` `failed`, reason `all_candidates_previously_failed` | every proposed action already failed in this incident | manual takeover |
| `security.gate` `rejected` repeatedly | see `docs/runbooks/security-gate-rejection.md` | — |
| `attempt.result` `recovered`, but `GET /widgets` fails | repair passed `/health/deep` with a wrong schema | manual takeover (`docs/runbooks/drop_widgets_table.md`), then remove the bad learned action (risky change) |

## Take over

Use "Manual takeover" in `docs/runbooks/README.md`. Stop `mi-agent` **before** repairing:
retries do not re-check health, so a repair made while MI runs is credited to MI's next
action and persisted as a learned success.

## Afterwards

Record the incident in `docs/WORKLOG.md` with the pattern above. A pattern seen in more
than one incident goes into the steering loop (S1–S6, `harness/governance/README.md`): a
guide, sensor, test, or skill change that would have prevented or caught it.
