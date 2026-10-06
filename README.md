# MI (Maintenance Intelligence) v1

Autonomous self-healing agent for a sandboxed SQLite-backed service.

- Agent is LLM-driven (`gpt-5.3-codex` by default) and performs monitoring, diagnosis, planning, action execution, and learning.
- Verifier is independent and is the only source of health truth.
- Supervisor owns lock, backoff, cap, and last-resort restore.
- Backups are isolated from the agent tool layer.
- v1 seeded recovery target: missing SQLite DB file (`db_file_missing`) with dynamic diagnosis.

## Architecture

- `src/mi_agent/agent`: monitor loop, diagnosis, selection, and execution.
- `src/mi_agent/tools`: allowlisted typed primitives with guardrails + snapshots/rollback.
- `src/mi_agent/verifier`: independent deep verification.
- `src/mi_agent/supervisor`: lock/backoff/cap and last-resort client.
- `src/mi_agent/policy`: RL value table + lessons.
- `src/mi_agent/skills`: skill distillation, matching, persistence, demotion/retirement.
- `src/mi_agent/api`: FastAPI API + SSE + static dashboard serving.

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
pytest -q
ruff check .
mypy --strict src
```

## LLM provider configuration

By default MI uses OpenAI Responses API with bearer auth.

- `MI_LLM_ENDPOINT` default: `https://api.openai.com/v1/responses`
- `MI_LLM_AUTH_MODE` default: `bearer`
- `OPENAI_API_KEY` (or `MI_OPENAI_API_KEY`)

For Azure-style endpoints, use full endpoint URL and API-key header mode:

```bash
export MI_LLM_ENDPOINT='https://<your-resource>.openai.azure.com/openai/responses?api-version=<your-api-version>'
export MI_LLM_AUTH_MODE='api_key'
export MI_LLM_API_KEY_HEADER='api-key'
export OPENAI_API_KEY='<your-azure-api-key>'
```

Set `MI_LLM_MODEL` to your deployment/model name expected by your endpoint.
If your endpoint is throttled, increase `MI_LLM_RETRIES` so MI keeps retrying LLM calls before giving up that attempt.

## Run demo (Docker Compose)

```bash
docker compose up --build
```

Services:
- Target app: `http://localhost:8080`
- MI API + dashboard: `http://localhost:8090`
- Last-resort restore service: `http://localhost:8070`

## Demo: trigger `db_file_missing`

1. Start stack.
2. Open dashboard: `http://localhost:8090`
3. Manually delete DB inside target container:

```bash
docker exec -it mi-target-app rm /target/app/data/app.db
```

4. MI detects failure on next monitor ping (default 5s), recovers, and learns a skill.
5. Delete DB again with the same command.
6. Observe MI match and run learned skill for faster recovery.

## API quick reference

- `GET /state`: current incident + epsilon settings.
- `GET /policy`: RL action values and lessons.
- `GET /skills`: learned skills + stats.
- `GET /incidents`: incident history events.
- `GET /events/stream`: SSE event stream.

## Safety properties

- LLM output is never executed as code/shell.
- LLM may only choose allowlisted primitive IDs with schema-validated parameters.
- Mutating actions snapshot before execution and support rollback.
- Paths are sandbox-scoped and backup paths are hard-denied (incl. traversal/symlink attempts).
- Agent cannot self-report success; verifier result gates resolution.

## v1 limitations

- Dynamic failure typing is inferred by diagnosis output and persisted in policy/skills.
- LLM client uses a minimal Responses API wrapper and expects compatible response JSON.
- Dashboard is intentionally minimal static HTML.
- Last-resort restore in Compose is HTTP-based; local tests may also use local restorer.
