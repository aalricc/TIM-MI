# MI v2 Results (current repository state)

## Evaluation mode

- **Mode:** replay-only (`mi-eval ... --replay`)
- **Why:** deterministic CI/offline execution without network dependence.
- **Implication:** these numbers validate pipeline behavior and reporting plumbing; they are not live-online
  model performance claims.

## Commands used

```bash
PYTHONPATH=src python -m mi_agent.eval.cli experiments \
  --replay \
  --replay-file artifacts/eval/replay.json \
  --seeds 5 \
  --output-dir artifacts/eval \
  --run-id phase8_experiments

PYTHONPATH=src python -m mi_agent.eval.cli run \
  --replay \
  --replay-file artifacts/eval/replay.json \
  --seeds 1 \
  --curriculum shuffled \
  --held-out-scenarios S17,S18 \
  --held-out-variants \
  --drift \
  --output-dir artifacts/eval \
  --run-id phase8_eval

PYTHONPATH=src python -m mi_agent.eval.cli report \
  --experiments-json artifacts/eval/phase8_experiments.json \
  --eval-json artifacts/eval/phase8_eval.json \
  --decision-log state/decision_log.jsonl \
  --skills-json state/skills.md \
  --output-dir artifacts/eval \
  --run-id phase8_report
```

## Headline metrics

From `artifacts/eval/phase8_experiments.json`:

- learning-enabled success mean: **0.9389**
- policy-off (no-learning ablation) success mean: **0.8789**
- skills-off success mean: **0.8589**
- held-out success (learning-enabled): **0.9600**
- drift recovery episodes (learning-enabled): **1.8**
- drift recovery episodes (policy-off): **3.2**
- red-team scenario S18 success (learning-enabled): **1.0**

From `artifacts/eval/phase8_eval.json`:

- episodes: **36**
- success_rate: **0.944**
- drift episodes: **18**
- split counts: `{"held_out_variant": 18, "train": 16, "held_out_scenario": 2}`

## Limits and caveats

- Replay-mode metrics are deterministic and bounded by the replay harness model.
- The dashboard/report plumbing is complete, but live-key online evaluation is not represented in this file.
- Claims are reported directly from generated artifacts; no threshold tuning was applied to force outcomes.

## Scenario-level limitations (current run)

The current `phase8_eval` scenario success map reports non-perfect solve rates for:

- **S02:** 0.5
- **S03:** 0.5

These scenarios are listed as currently not fully solved in this replay run and are carried into README
limitations until improved.
