# Threshold Justification

This document explains why the current thresholds exist, where each is enforced, and how each is validated.

## 1) Decision Thresholds (Safety / Action Gating)

| Setting | Default | Purpose | If too low | If too high | Enforcement |
|---|---:|---|---|---|---|
| `MI_JEV_USABLE_THRESHOLD` | `0.65` | Minimum JEV noul confidence for auto-execution | Executes weak/unsafe candidates more often | Over-blocks valid actions (`decision.unusable`) and slows recovery | `src/mi_agent/decision/client.py` |
| `MI_CLASSMEM_JOIN_THRESHOLD` | `0.82` | Requires strong signature similarity (`s1`) before class reuse | Misclassifies incidents and applies wrong past actions | Creates too many new classes; weak reuse | `src/mi_agent/class_memory.py` |
| `MI_CLASSMEM_MARGIN_DELTA` | `0.08` | Requires separation (`s1 - s2`) to avoid ambiguous class matches | Ambiguous matches pass and can choose wrong class | Fast path rarely triggers due to strict ambiguity filter | `src/mi_agent/class_memory.py` |
| `MI_CLASSMEM_TAU_CONF` | `0.80` | Requires class champion LCB confidence before fast path | Promotes unstable actions too early | Fast path under-used; more full-loop fallbacks | `src/mi_agent/class_memory.py` |
| `MI_CLASSMEM_N_MIN` | `3` | Minimum attempts before an action is considered reusable | One-off successes become trusted too soon | Learning is delayed and reuse is slow | `src/mi_agent/class_memory.py` |
| `MI_CLASSMEM_COOLDOWN_SECONDS` | `30` | Temporary block after failed action in a class | Immediate repeated failures/retries | Slow recovery from transient failures | `src/mi_agent/class_memory.py` |
| `beta_lcb z` | `0.5` | Conservativeness factor in LCB estimate | Optimistic confidence estimate | Overly pessimistic confidence estimate | `src/mi_agent/class_memory.py` |

Notes:
- `0.65` is a policy default (safety-biased midpoint), not a mathematically fixed constant.
- Class-memory gates are intentionally layered: similarity (`s1`), ambiguity (`margin`), sample size (`n_min`), confidence (`tau_conf`), and cooldown.

## 2) Operational Thresholds (Stability / Performance)

| Setting | Default | Purpose | Enforcement |
|---|---:|---|---|
| `MI_MONITOR_INTERVAL_SECONDS` | `1.0` | Polling cadence for monitor loop | `src/mi_agent/config.py` |
| `MI_REQUEST_TIMEOUT_SECONDS` | `5.0` | Tool/verifier HTTP timeout ceiling | `src/mi_agent/config.py` |
| `MI_LLM_TIMEOUT_SECONDS` | `20.0` | LLM request timeout ceiling | `src/mi_agent/config.py` |
| `MI_JEV_TIMEOUT_SECONDS` | `8.0` | JEV request timeout ceiling | `src/mi_agent/config.py` |
| `MI_RAG_TOP_K` | `5` | Number of retrieval chunks for grounding | `src/mi_agent/config.py` |

These are not safety decision boundaries by themselves, but they shape reliability/latency and can indirectly affect recovery quality.

## 3) Test Evidence (What Is Actually Verified)

### JEV threshold behavior
- Single-candidate path does not require Choice and still returns noul/score: `tests/test_jev_client.py`
- Below-threshold noul is rejected with `usable_probability_below_threshold:*`: `tests/test_jev_client.py`

### Class-memory threshold behavior
- Reject on low similarity (`s1_below_threshold`): `tests/test_class_memory.py`
- Reject on low margin (`margin_below_delta`): `tests/test_class_memory.py`
- Reject on low confidence (`lcb_below_tau`): `tests/test_class_memory.py`
- Cooldown and `n_min` gating behavior covered: `tests/test_class_memory.py`

### Engine integration behavior
- Class-memory configuration and gating flow in engine path: `tests/test_class_memory_engine.py`

## 4) Calibration / Retuning Procedure

When tuning thresholds, change one knob at a time and compare against baseline runs.

Recommended metrics:
- `decision.unusable` rate
- incident recovery success rate
- average/p95 recovery latency
- retries per incident
- fast-path usage rate (`source=class_memory` vs `source=full_loop`)

Safe tuning steps:
1. Pick one threshold to tune.
2. Adjust by small increments (e.g., `±0.05` for probability thresholds).
3. Run staged evaluation/smoke workloads.
4. Accept only if success is stable and latency improves (or safety improves without unacceptable latency regressions).

## 5) Project-closure rationale template

Use this wording in final report:

> Thresholds were selected as conservative defaults for safe autonomous recovery and were validated in unit + integration flows. We used JEV noul confidence (`MI_JEV_USABLE_THRESHOLD`) to gate auto-execution and class-memory gates (`JOIN_THRESHOLD`, `MARGIN_DELTA`, `TAU_CONF`, `N_MIN`, `COOLDOWN_SECONDS`) to prevent premature reuse of unstable actions. We confirmed expected reject/allow behavior in dedicated threshold tests and end-to-end recovery runs, and retained all thresholds as configurable runtime policy.

