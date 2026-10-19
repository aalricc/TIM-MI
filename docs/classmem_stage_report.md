# Class-Memory Stage Report

## Method

- Harness: `scripts/classmem_stage_benchmark.py` (synthetic failure-injection, production agent loop entry via `RecoveryAgent._attempt_recovery`).
- Command run: `python scripts/classmem_stage_benchmark.py --incidents 80 --json`.
- Incident mix: 80 incidents with alternating identical/similar normalized error families (same structural template, different slots).
- Security behavior in harness: every executed action passes through a simulated security gate; blocked unsafe actions are counted.

## Stage results

| Stage | Attempts (mean) | Latency p50 ms | Latency p95 ms | Action-key entropy | Success rate | Blocked/unsafe | Fast-path hit | Fast-path fail | Classes | skills sections |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Stage 0 (baseline) | 4.000 | 2.407 | 3.038 | 1.500 | 1.000 | 80 | 0.000 | 0.000 | 0 | 0 |
| Stage 1 (observe only) | 4.000 | 4.969 | 6.041 | 1.500 | 1.000 | 80 | 0.000 | 0.000 | 1 | 1 |
| Stage 2 (dedupe + tie-break) | 2.000 | 2.695 | 3.264 | 1.000 | 1.000 | 80 | 0.000 | 0.000 | 1 | 1 |
| Stage 3 (store + skills upsert) | 2.000 | 2.768 | 3.322 | 1.000 | 1.000 | 80 | 0.000 | 0.000 | 1 | 1 |
| Stage 4 (fast path on) | 1.075 | 1.338 | 2.525 | 0.365 | 1.000 | 6 | 0.860 | 0.000 | 1 | 1 |

## Gates

- Stage 0 baseline captured and repeatable: **pass**.
- Stage 1 normalization observation (no selection/authority bypass): **pass**.
- Stage 2 entropy drop without success regression: **pass** (`1.500 -> 1.000`, success unchanged).
- Stage 3 one skills section per class and stable class IDs: **pass**.
  - `class_id_stability = true` from forward vs reverse ingestion in benchmark.
  - `skills_section_count == class_count == 1`.
- Stage 4 conservative fast path improves repeats without safety regression: **pass**.
  - Attempts: `2.000 -> 1.075`.
  - Latency p50: `2.695ms -> 1.338ms`.
  - Success: unchanged at `1.000`.
  - Blocked/unsafe actions: reduced (`80 -> 6`).

## Stage 5 recommendation

Stage 5 (adaptive thresholds, merges, ANN index, decay) is **not warranted** with current evidence.
Stage 4 already reaches the target direction on attempts, latency, variance, and safety metrics on repeated incidents with no success-rate regression.
