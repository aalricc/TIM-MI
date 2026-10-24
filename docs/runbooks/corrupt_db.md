# Runbook: database file corrupt (`db_corrupt`)

Injected with `curl -X POST http://localhost:8080/ops/fault/corrupt_db`, which overwrites
`app.db` with the bytes `not-a-valid-sqlite-db`.

## Symptoms

- `GET /health/deep` → 500, `detail.error` = `db_corrupt` (`file is not a database`).
- `GET /ops/db/status` → `"exists": true`, `"error": "db_corrupt"`.
- Target log: `"fault injected"` with `"fault": "corrupt_db"` (if injected), then `error`
  heartbeats.

## Expected MI behaviour

WHY reports a corrupt database. `run_sql` cannot help (SQLite cannot open the file). The
fix is restoring a valid file. The backup job validates every copy, so the corrupt file is
never kept and `request_backup_restore` with `latest` restores the last valid copy.

Before backup validation existed, the job copied the corrupt file every 10 s. Recorded runs
(`state/events.jsonl`) from that time: 2 of 3 corrupt_db incidents recovered on the first
`latest` restore; the third (`inc-00e04644b7`) restored `latest` 17 times without
recovering, each time picking up a newer corrupt copy, and has no `incident.recovered` in
the log. If the volume still holds backups from the old job, prefer an explicit
`backup_id`.

## Approved recovery actions

| Rank | Action | Effect |
| --- | --- | --- |
| 1 | `request_backup_restore` with `backup_id` `latest` | restores the last validated copy |
| 2 | `request_backup_restore` with `backup_id` `app-<epoch>` from `scripts/find_known_good_backup.py` | explicit pre-fault backup; use when old unvalidated backups may be newest |
| 3 | `replace_file` with `"source_path": "backups/app-<epoch>.db"` and `"destination_path": "data/app.db"` | same, explicit copy |

Not approved: `run_sql`; `restart_service` (always fails here). Data written after the
restored backup's time is lost.

## Manual recovery

Follow "Manual takeover" in `docs/runbooks/README.md`, then:

```bash
backup="$(docker compose exec -T mi-agent python - < scripts/find_known_good_backup.py)"
echo "$backup"
docker compose exec mi-agent cp "$backup" /target/app/data/app.db
```

The script skips any corrupt copies left by the old job and prints the newest valid backup
taken at or before the last healthy heartbeat. Then run the post-check
(`/health/deep` 200 and `GET /widgets` ok).

## Escalate

No valid backup, or no recovery after 3 attempts: `docs/runbooks/stuck-incident.md`.
