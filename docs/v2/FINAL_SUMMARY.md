# MI v2 Final Summary

MI v2 in this repository is now aligned to a live-first autonomous recovery loop:

- no operator handoff path
- no attempt/hour/time recovery caps
- mandatory model `gpt-5.3-codex`
- endpoint configured by `MI_LLM_ENDPOINT`
- transient LLM errors retried with backoff+jitter
- permanent auth/permission/config errors fail closed with clear messages
- verifier remains the only recovery success gate

## Primary commands

```bash
./scripts/v2_gate.sh 8
pytest -q
ruff check .
mypy --strict src
```

Live checks (requires credentials):

```bash
OPENAI_API_KEY=<key> MI_LLM_ENDPOINT=<endpoint> pytest -q -m live
```

## Notes

- Evaluation summaries must be generated from actual runs.
- When no live run was executed, reports must explicitly state that live execution was not run.
