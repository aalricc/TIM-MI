# MI v2 Baseline

This baseline document tracks the current runtime invariants for MI v2.

## Runtime invariants

- Autonomous loop continues until verifier success, cancellation/shutdown, or permanent error.
- No operator escalation branch.
- No attempt/hour/wall-clock stop budgets in recovery control flow.
- LLM model is fixed to `gpt-5.3-codex`.
- LLM endpoint comes from `MI_LLM_ENDPOINT`.
- Tool execution is allowlisted and schema-validated.

## Validation commands

```bash
pytest -q
ruff check .
mypy --strict src
```

Live validation requires explicit credentials and endpoint configuration.
