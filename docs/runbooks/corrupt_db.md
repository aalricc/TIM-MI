# Runbook: database file corrupt (`db_corrupt`)

Injected with `curl -X POST http://localhost:8080/ops/fault/corrupt_db` (stack started with
`TARGET_TEST_HOOKS=1`), which overwrites
`app.db` with the bytes `not-a-valid-sqlite-db`.

## Symptoms

- `GET /health/deep` → 500, `detail.error` = `db_corrupt` (`file is not a database`).
- `GET /ops/db/status` → `"exists": true`, `"error": "db_corrupt"`.
- Target log: `"fault injected"` with `"fault": "corrupt_db"` (if injected), then `error`
  heartbeats.

## Expected MI behaviour

WHY reports a corrupt database. `run_sql` cannot help (SQLite cannot open the file). The
fix is restoring a valid file. The backup job validates every copy, so the corrupt file is
never kept and `request_backup_restore` with any catalog backup taken before the fault
restores a valid copy.

Before backup validation existed, the job copied the corrupt file every 10 s. Recorded runs
(`state/events.jsonl`) from that time: 2 of 3 corrupt_db incidents recovered on the first
`latest` restore; the third (`inc-00e04644b7`) restored `latest` 17 times without
recovering, each time picking up a newer corrupt copy, and has no `incident.recovered` in
the log. If the volume still holds backups from the old job, use the id the finder
prints. MI no longer mounts the backups volume and has no `latest` shortcut: it lists
backups with `GET /ops/backups` and restores one with
`POST /ops/backups/{backup_id}/restore`.

## Approved recovery actions

| Rank | Action | Effect |
| --- | --- | --- |
| 1 | `request_backup_restore` with a `backup_id` from the target's catalog (`GET /ops/backups`) taken before the fault | restores that copy; the backup job keeps only validated copies |
| 2 | `request_backup_restore` with the `app-<epoch>` id printed by `scripts/find_known_good_backup.py` | explicit pre-fault backup; use when old unvalidated backups may be newest |

Not approved: `run_sql`; `restart_service` (always fails here). Data written after the
restored backup's time is lost.

## Manual recovery

Follow "Manual takeover" in `docs/runbooks/README.md`, then:

```bash
backup="$(docker compose exec -T backup-job python /scripts/find_known_good_backup.py)"
echo "$backup"
curl -s -X POST "http://localhost:8080/ops/backups/$(basename "$backup" .db)/restore"
```

The script skips any corrupt copies left by the old job and prints the newest valid backup
taken at or before the last healthy heartbeat. Then run the post-check
(`/health/deep` 200 and `GET /widgets` ok).

## Escalate

No valid backup, or no recovery after 3 attempts: `docs/runbooks/stuck-incident.md`.
