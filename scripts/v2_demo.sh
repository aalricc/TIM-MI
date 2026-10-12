#!/usr/bin/env bash

set -euo pipefail

OUTPUT_DIR="${1:-artifacts/eval}"
REPLAY_FILE="${2:-${OUTPUT_DIR}/replay.json}"

mkdir -p "${OUTPUT_DIR}"

if [[ ! -f "${REPLAY_FILE}" ]]; then
  python - <<'PY' "${REPLAY_FILE}"
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps([{"response": "plan"}] * 6000), encoding="utf-8")
PY
fi

MI_EVAL=(python -m mi_agent.eval.cli)

PYTHONPATH=src "${MI_EVAL[@]}" run --replay \
  --replay-file "${REPLAY_FILE}" \
  --seeds 1 \
  --curriculum shuffled \
  --held-out-scenarios S17,S18 \
  --held-out-variants \
  --drift \
  --output-dir "${OUTPUT_DIR}" \
  --run-id phase8_demo_pass1

PYTHONPATH=src "${MI_EVAL[@]}" run --replay \
  --replay-file "${REPLAY_FILE}" \
  --seeds 1 \
  --curriculum shuffled \
  --held-out-scenarios S17,S18 \
  --held-out-variants \
  --drift \
  --output-dir "${OUTPUT_DIR}" \
  --run-id phase8_demo_pass2

PYTHONPATH=src "${MI_EVAL[@]}" experiments --replay \
  --replay-file "${REPLAY_FILE}" \
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

episode_results = pass2.get("episode_results", [])
first_actions = []
repeat_actions = []
seen = set()
for item in episode_results:
    scenario_id = item.get("scenario_id")
    action_count = item.get("action_count")
    if not isinstance(scenario_id, str) or not isinstance(action_count, (int, float)):
        continue
    if scenario_id in seen:
        repeat_actions.append(float(action_count))
    else:
        seen.add(scenario_id)
        first_actions.append(float(action_count))

first_mean = sum(first_actions) / len(first_actions) if first_actions else 0.0
repeat_mean = sum(repeat_actions) / len(repeat_actions) if repeat_actions else 0.0
speedup = experiments["repeat_incident"]["skill_reuse_speedup"]["learning_enabled"]
print("v2 demo complete")
print(f"first_injection_actions={first_mean:.3f}")
print(f"repeat_injection_actions={repeat_mean:.3f}")
print(f"repeat_incident_speedup={speedup:.3f}")
print(f"artifacts={output_dir}")
PY
