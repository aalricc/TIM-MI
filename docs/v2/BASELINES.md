# MI v2 Baselines

generated_at: 2026-10-07T12:19:04Z
source_command: `mi-eval baseline --seeds 2 --curriculum shuffled --held-out-scenarios S17,S18 --held-out-variants --drift --output docs/v2/BASELINES.md`

## Source Run
- episodes: 54
- curriculum: shuffled
- seeds: 2
- held_out_scenarios: ['S17', 'S18']
- held_out_variants: True
- drift: True

## Baseline Metrics

| baseline | success_rate | mttr_seconds | actions_per_incident | last_resort_rate |
|---|---:|---:|---:|---:|
| random_over_allowlist | 0.3333 | 8.4311 | 4.2778 | 0.0741 |
| v1_agent | 0.6667 | 8.7725 | 4.6667 | 0.0185 |
