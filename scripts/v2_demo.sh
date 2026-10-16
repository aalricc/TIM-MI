#!/usr/bin/env bash

set -euo pipefail

OUTPUT_DIR="${1:-artifacts/eval}"

mkdir -p "${OUTPUT_DIR}"

MI_EVAL=(python -m mi_agent.eval.cli)

PYTHONPATH=src "${MI_EVAL[@]}" run \
  --seeds 1 \
  --curriculum shuffled \
  --held-out-scenarios S17,S18 \
  --held-out-variants \
  --drift \
  --output-dir "${OUTPUT_DIR}" \
  --run-id phase8_demo_pass1

PYTHONPATH=src "${MI_EVAL[@]}" run \
  --seeds 1 \
  --curriculum shuffled \
  --held-out-scenarios S17,S18 \
  --held-out-variants \
  --drift \
  --output-dir "${OUTPUT_DIR}" \
  --run-id phase8_demo_pass2

PYTHONPATH=src "${MI_EVAL[@]}" experiments \
  --seeds 5 \
  --output-dir "${OUTPUT_DIR}" \
  --run-id phase8_demo_experiments

python - <<'PY' "${OUTPUT_DIR}"
import json
import sys
from pathlib import Path

output_dir = Path(sys.argv[1])
pass1 = json.loads((output_dir / "phase8_demo_pass1.json").read_text(encoding="utf-8"))
pass2 = json.loads((output_dir / "phase8_demo_pass2.json").read_text(encoding="utf-8"))
experiments = json.loads(
    (output_dir / "phase8_demo_experiments.json").read_text(encoding="utf-8")
)

print("v2 demo complete")
print(f"pass1_live={pass1.get('live_run_executed')}")
print(f"pass2_live={pass2.get('live_run_executed')}")
summary = experiments.get("summary", {})
print(f"experiments_live={summary.get('live_run_executed')}")
print(f"artifacts={output_dir}")
PY
