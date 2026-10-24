# Threat Model

## Assets

- Target SQLite data (`MI_DB_FILE`).
- Schema and runtime files under `MI_APP_ROOT`.
- Backup snapshots in the `backups_data` volume (read-only to the target, which lists and
  restores them; MI reaches them only through `GET /ops/backups` and
  `POST /ops/backups/{backup_id}/restore`).
- Recovery policy and dynamic tool catalog in `state/`.

## Trust boundaries

- LLM output is untrusted and treated as data.
- JEV output is untrusted and treated as data.
- RAG chunks are untrusted external knowledge.
- Dynamic tool specs are never executed without security validation.

## Controls

- Startup config gate enforces required OpenAI, JEV, and Pinecone credentials.
- JEV decision gate selects one candidate action before registration/execution.
- Security gate checks operation allowlist and parameter schema.
- Path guard blocks escaping `MI_SANDBOX_ROOT` and `MI_APP_ROOT` for file actions.
- Verifier independently confirms health after each dynamic action.
- Only verifier-confirmed winning tool is persisted.

## Residual risk

- `run_sql` can issue destructive SQL if diagnosis/planning is wrong.
- Incorrect JEV thresholds can over-block recovery or under-block risky actions.
- Default `restart_service` behavior is a no-op unless an integration hook is configured.
- RAG quality depends on Pinecone index quality and recency.
- Verification is only as strong as the target's `/health/deep`. It now checks integrity,
  the `widgets` table, and the columns the endpoints read; it does not exercise the
  endpoints themselves. Learned state recorded before the column check contains a tool and
  class template that create `widgets` with the wrong columns, and SQL templates that
  create unrelated tables.
- A repair made outside MI during an attempt (after the pre-retry health check, before
  verification) is credited to that attempt's action and persisted. Repairs between
  attempts close the incident without learning.
- The backup job validates each copy before keeping it and retains the newest 360, but a
  database that passes validation while logically wrong (bad data) is still backed up.

## Operational response

Fault handling, manual takeover, and post-incident review: `docs/runbooks/README.md`.
