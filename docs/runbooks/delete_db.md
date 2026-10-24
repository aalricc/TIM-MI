# Runbook: database file missing (`db_missing`)

Injected with `curl -X POST http://localhost:8080/ops/fault/delete_db` (stack started with
`TARGET_TEST_HOOKS=1`).

## Symptoms

- `GET /health/deep` → 500, `detail.error` = `db_missing`, `detail.status.exists` = `false`.
- `GET /ops/db/status` → `"exists": false`, `"error": "db_missing"`.
- Target log: `"fault injected"` with `"fault": "delete_db"` (if injected), then `error`
  heartbeats.

The target does not recreate the file on its own: `target/app/main.py::ensure_db` runs only
at startup.

## Expected MI behaviour

WHY reports a missing database. `run_sql` cannot help (the executor returns `db_missing`
when the file does not exist). The expected action is a backup restore.

Recorded run (`inc-a20e7ee26b`): WHY `database_file_deleted`; attempt 1 chose `run_sql`
(failed with `db_missing`), attempt 2 restored `latest` and recovered.

## Approved recovery actions

| Rank | Action | Effect |
| --- | --- | --- |
| 1 | `request_backup_restore` with a `backup_id` from the target's catalog (`GET /ops/backups`) | the backup job stops copying while the file is missing, so the newest backup normally predates the fault; check its `created_at` against the fault if a previous incident is still open |

Not approved: `run_sql` (fails on a missing file); `restart_service` (always fails here).
Data written after the restored backup's time is lost in every option.

## Manual recovery

Follow "Manual takeover" in `docs/runbooks/README.md`, then:

```bash
backup="$(docker compose exec -T backup-job python /scripts/find_known_good_backup.py)"
echo "$backup"
curl -s -X POST "http://localhost:8080/ops/backups/$(basename "$backup" .db)/restore"
```

Last resort when no valid backup exists (creates an **empty** database from
`target/app/schema.sql`): `docker compose restart target-app`.

Then run the post-check (`/health/deep` 200 and `GET /widgets` ok).

## Escalate

No valid backup, or no recovery after 3 attempts: `docs/runbooks/stuck-incident.md`.
