# MI Blockers

## Current environment blockers

1. Live LLM evaluation requires valid external credentials and endpoint access.
2. CI intentionally does not execute live-model tests because it runs without secrets.

## How to run live checks locally

```bash
OPENAI_API_KEY=<key> MI_LLM_ENDPOINT=<endpoint> pytest -q -m live
```
