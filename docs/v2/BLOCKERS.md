## Active blockers

- `git commit` workflow is blocked because `.git` is read-only in this environment (`fatal: Unable to create .../.git/index.lock: Operation not permitted`).
  - Required human action: grant write access to `.git` (or run commits externally) so required task commits can be created.
  - Repro command: `git commit -m "v2(P0-2): gate scaffold"`

- Live LLM validation was not run in this environment (replay-only evaluation used for Phase 8 results).
  - Required human action: provide valid LLM credentials/network access and run live smoke validation.
  - Repro command: `MI_RUN_LIVE_LLM_TEST=1 OPENAI_API_KEY=<key> PYTHONPATH=src pytest -q tests/integration/test_live_llm_smoke.py`
