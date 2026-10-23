# Threat Model

## Assets

- Target SQLite data (`MI_DB_FILE`).
- Schema and runtime files under `MI_APP_ROOT`.
- Backup snapshots under `MI_BACKUPS_ROOT`.
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
- The verifier checks only `/health/deep` (widgets table present, integrity ok), not the
  column set, so a repair that recreates `widgets` with the wrong columns is verified and
  persisted as a learned success. Recorded learned state contains such a tool and class
  template, and SQL templates that create unrelated tables.
- Retries do not re-check health; a human repair made while an incident is open is
  credited to MI's next action and persisted.
- The backup job copies the database every 10 s regardless of its state and keeps every
  copy, so `latest` backups can contain the fault and disk use grows without bound.

## Operational response

Fault handling, manual takeover, and post-incident review: `docs/runbooks/README.md`.
