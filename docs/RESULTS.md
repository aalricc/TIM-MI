# MI Results

This file reports the current repository's evaluation mode and the command paths used to generate artifacts.

## Evaluation policy

- Reports must come from actual executed runs.
- If a live run is not executed, the report must explicitly say so.
- No replay file is used as a substitute for live LLM execution.

## Commands

Offline (no live model execution):

```bash
PYTHONPATH=src python -m mi_agent.eval.cli run \
  --seeds 1 \
  --curriculum shuffled \
  --held-out-scenarios S17,S18 \
  --held-out-variants \
  --drift \
  --output-dir artifacts/eval \
  --run-id phase8_eval

PYTHONPATH=src python -m mi_agent.eval.cli experiments \
  --seeds 5 \
  --output-dir artifacts/eval \
  --run-id phase8_experiments
```

Live (real model + preflight):

```bash
OPENAI_API_KEY=<key> MI_LLM_ENDPOINT=<endpoint> \
PYTHONPATH=src python -m mi_agent.eval.cli run --live \
  --seeds 1 \
  --curriculum shuffled \
  --held-out-scenarios S17,S18 \
  --held-out-variants \
  --drift \
  --output-dir artifacts/eval \
  --run-id phase8_live_eval
```

## Current state in this repository

- Latest committed defaults support offline summary generation without secrets.
- Live metrics are only valid when commands are actually executed with working credentials.
- If live artifacts are missing, treat live performance as **not run**.
