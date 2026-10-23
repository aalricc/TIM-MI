# Runbook: widgets table missing (`schema_invalid`)

Injected with `curl -X POST http://localhost:8080/ops/fault/drop_widgets_table`.

## Symptoms

- `GET /health/deep` → 500, `detail.error` = `schema_invalid`,
  `detail.status.widgets_table_present` = `false`, `integrity` = `["ok"]`.
- A table that exists with the wrong columns is also `schema_invalid`, with
  `detail.status.widgets_columns_missing` naming them (for example after a bad repair).
- `GET /widgets` and `POST /widgets` → 500 `schema_invalid` (`no such table: widgets`).
- Target log: `"fault injected"` with `"fault": "drop_widgets_table"` (if injected), then
  `"heartbeat"` lines at level `error`.

All rows in `widgets` are gone; the database file itself is intact.

## Expected MI behaviour

WHY classifies a missing required table (recorded runs use labels such as
`schema_drift_missing_required_table` and `schema_invalid_missing_widgets_table`). This is
the most practised fault (41 of 46 injections in `target/app/app.log`); class memory
usually has a confident
action, so most recoveries are a single fast-path `run_sql`.

## Approved recovery actions

| Rank | Action | Effect |
| --- | --- | --- |
| 1 | `run_sql` recreating `widgets` exactly as in `target/app/schema.sql` (`id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP`), idempotent (`CREATE TABLE IF NOT EXISTS`) | fast; **table is empty afterwards** (rows lost by the fault stay lost) |
| 2 | `request_backup_restore` with `backup_id` `latest` (the last validated copy, from before the drop) or an explicit `app-<epoch>` | restores rows as of that backup; loses writes between the backup and the fault |

Not approved: SQL that creates `widgets` with a different column set (now fails
`/health/deep` with `widgets_columns_missing`); SQL touching any other table (recorded runs
show invented tables such as `fault_injection_config`); `restart_service` (always fails
here).

## Manual recovery

Follow "Manual takeover" in `docs/runbooks/README.md`. Choose one:

```bash
# Keep data: restore the newest valid pre-fault backup
backup="$(docker compose exec -T mi-agent python - < scripts/find_known_good_backup.py)"
echo "$backup"                                          # check before copying
docker compose exec mi-agent cp "$backup" /target/app/data/app.db

# Or recreate an empty table from the schema
docker compose exec -T mi-agent python - <<'EOF'
import sqlite3
from pathlib import Path
schema = Path("/target/app/schema.sql").read_text(encoding="utf-8")
with sqlite3.connect("/target/app/data/app.db") as conn:
    conn.executescript(schema.replace("CREATE TABLE widgets", "CREATE TABLE IF NOT EXISTS widgets", 1))
EOF
```

Then run the post-check (`/health/deep` 200 and `GET /widgets` ok).

## Escalate

If MI has not recovered after 3 attempts, or a recovered table fails the `GET /widgets`
post-check, follow `docs/runbooks/stuck-incident.md`.
