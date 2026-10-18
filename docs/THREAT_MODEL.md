# Threat Model

## Assets

- Target SQLite data (`MI_DB_FILE`).
- Schema and runtime files under `MI_APP_ROOT`.
- Backup snapshots under `MI_BACKUPS_ROOT`.
- Recovery policy and dynamic tool catalog in `state/`.

## Trust boundaries

- LLM output is untrusted and treated as data.
- RAG chunks are untrusted external knowledge.
- Dynamic tool specs are never executed without security validation.

## Controls

- Startup config gate enforces Pinecone requirements when RAG is enabled.
- Security gate checks operation allowlist and parameter schema.
- Path guard blocks escaping `MI_SANDBOX_ROOT` and `MI_APP_ROOT` for file actions.
- Verifier independently confirms health after each dynamic action.
- Only verifier-confirmed winning tool is persisted.

## Residual risk

- `run_sql` can issue destructive SQL if diagnosis/planning is wrong.
- Default `restart_service` behavior is a no-op unless an integration hook is configured.
- RAG quality depends on Pinecone index quality and recency.
