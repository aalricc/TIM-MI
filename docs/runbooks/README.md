# Runbooks

Operator procedures for the compose stack in `docker-compose.yml`. Each runbook says what
MI should do on its own, which recovery actions are approved, and how a human takes over.
Policy context: `docs/THREAT_MODEL.md`, `harness/governance/README.md`.

| Runbook | Use when |
| --- | --- |
| `docs/runbooks/drop_widgets_table.md` | `/health/deep` error `schema_invalid` (widgets table missing) |
| `docs/runbooks/delete_db.md` | error `db_missing` (database file gone) |
| `docs/runbooks/corrupt_db.md` | error `db_corrupt` (file is not a valid SQLite database) |
| `docs/runbooks/security-gate-rejection.md` | `security.gate` = `rejected` in the event log |
| `docs/runbooks/stuck-incident.md` | an incident is not recovering, or MI itself is failing |

## How this deployment behaves (applies to every runbook)

- **Monitor and verifier both use `GET /health/deep`.** It checks integrity, that
  `widgets` exists, and that it has the columns the widget endpoints read
  (`target/app/main.py::REQUIRED_WIDGET_COLUMNS`); a missing column is `schema_invalid`
  with `widgets_columns_missing` listing it. Still run the post-check below: it exercises
  the real endpoint.
- **Backups.** `target/backup/backup.sh` copies `app.db` every 10 s to a temporary file,
  validates the copy (`scripts/find_known_good_backup.py --check`), and keeps it as
  `app-<epoch>.db` in the `backups_data` volume only if it passes; it keeps the newest 360
  (`BACKUP_RETENTION`, about one hour). So `latest` is the last valid copy. Backups written
  by the older job (before this change) were copied unvalidated and can contain faults;
  `scripts/find_known_good_backup.py` skips those.
- **`restart_service` always fails here.** No restart hook is configured
  (`src/mi_agent/runtime.py::build_agent` builds `ToolExecutor` without hooks), so it returns
  `no_restart_hook_configured`. `request_backup_restore` works (built-in file copy).
- **Retries re-check health first.** Before each retry MI checks the monitor; if the target
  is already healthy it closes the incident (`incident.closed` with `resolution` =
  `healthy_before_retry`) and records nothing. A repair that lands *during* an attempt
  (after that check, before verification) can still be credited to the in-flight action,
  so stopping MI before a manual repair remains the clean procedure.
- **Startup.** The runtime refuses to start unless class memory, JEV, and RAG are enabled
  and their credentials set. Create `.env` from `.env.example` for the credentials.

## Common commands

Run from the repository root.

```bash
# Target state
curl -s http://localhost:8080/health/deep          # 500 + {"detail": {"error": ...}} when unhealthy
curl -s http://localhost:8080/ops/db/status
tail -n 20 target/app/app.log                      # JSON lines; "fault injected" marks injections

# MI state
curl -s http://localhost:8090/agent/status         # workflow_state, incident (attempts, last_tool_error)
docker compose logs --tail 50 mi-agent

# Backups (inside the mi-agent container, which mounts the backups volume)
docker compose exec mi-agent ls -lt /target/backups | head
docker compose exec -T mi-agent python - < scripts/find_known_good_backup.py
```

`scripts/find_known_good_backup.py` prints the newest backup taken at or before the last
healthy heartbeat that passes integrity and has `widgets(id, name, created_at)`. Use
`--before <ISO-8601 with offset | epoch>` to set the cutoff yourself:
`docker compose exec -T mi-agent python - --before 1792603000 < scripts/find_known_good_backup.py`.

## Manual takeover (all runbooks)

1. `docker compose stop mi-agent` (keeps your repair from overlapping an in-flight attempt,
   which would credit it to MI; the open incident is in memory only and is discarded).
2. Repair using the runbook's manual procedure.
3. Post-check, both must succeed:
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/health/deep   # 200
   curl -s 'http://localhost:8080/widgets?limit=1'                              # {"ok":true,...}
   ```
4. `docker compose start mi-agent`. The first monitor check sees a healthy target, so no
   incident is opened.
5. Record the incident in `docs/WORKLOG.md` and start the steering loop (S1–S6 in
   `harness/governance/README.md`) if the fault class recurs.

## After every incident (MI-recovered or manual)

- Run the post-check above. A `/health/deep` 200 alone is not enough.
- Review what MI learned: `curl -s http://localhost:8090/agent/tool-catalog`,
  `state/skills.md`, and the incident's decisions
  (`python -m harness.metrics.kpis state/events.jsonl` for counts). Learned actions that
  touch tables other than `widgets`, or create `widgets` without the columns in
  `target/app/schema.sql`, must not be reused. Removing them is a hand edit of runtime
  state: a risky change (approval required).
