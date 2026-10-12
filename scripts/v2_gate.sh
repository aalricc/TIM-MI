#!/usr/bin/env bash

set -u

usage() {
  echo "Usage: scripts/v2_gate.sh <phase-number 0-8> [--exit]" >&2
}

if [[ $# -lt 1 || $# -gt 2 ]]; then
  usage
  exit 2
fi

phase="$1"
mode="task"
if [[ $# -eq 2 ]]; then
  if [[ "$2" != "--exit" ]]; then
    usage
    exit 2
  fi
  mode="exit"
fi

if ! [[ "$phase" =~ ^[0-8]$ ]]; then
  usage
  exit 2
fi

phase_marker="phase${phase}"
gate_failed=0
halted=0

mi-eval() {
  local mi_eval_bin
  mi_eval_bin="$(type -P mi-eval || true)"

  if [[ -n "$mi_eval_bin" ]]; then
    "$mi_eval_bin" "$@"
    return
  fi

  PYTHONPATH="src${PYTHONPATH:+:${PYTHONPATH}}" python -m mi_agent.eval.cli "$@"
}

run_check() {
  local label="$1"
  shift
  if [[ $halted -eq 1 ]]; then
    echo "FAIL ${label} (skipped after earlier failure)"
    return
  fi

  if "$@"; then
    echo "PASS ${label}"
  else
    echo "FAIL ${label}"
    gate_failed=1
    halted=1
  fi
}

phase_marker_expr() {
  local n="$1"
  local result=""
  local i
  for ((i = 0; i <= n; i++)); do
    if [[ -n "$result" ]]; then
      result+=" or "
    fi
    result+="phase${i}"
  done
  printf '%s' "$result"
}

check_progress_phase_exit() {
  python - "$phase" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

phase = int(sys.argv[1])
progress_path = Path("docs/v2/PROGRESS.md")
if not progress_path.exists():
    print("docs/v2/PROGRESS.md missing", file=sys.stderr)
    raise SystemExit(1)

lines = progress_path.read_text(encoding="utf-8").splitlines()
heading = f"## Phase {phase}"
try:
    start = lines.index(heading)
except ValueError:
    print(f"Missing heading: {heading}", file=sys.stderr)
    raise SystemExit(1)

end = len(lines)
for idx in range(start + 1, len(lines)):
    if lines[idx].startswith("## Phase "):
        end = idx
        break

task_re = re.compile(rf"^- \[(?P<done>[ xX])\] P{phase}-\d+\b")
tasks: list[tuple[str, bool]] = []
for line in lines[start + 1 : end]:
    match = task_re.match(line)
    if match is None:
        continue
    done = match.group("done").lower() == "x"
    tasks.append((line, done))

if not tasks:
    print(f"No Phase {phase} tasks found", file=sys.stderr)
    raise SystemExit(1)

unchecked = [line for line, done in tasks if not done]
if unchecked:
    print(f"Unchecked tasks remain in Phase {phase}", file=sys.stderr)
    raise SystemExit(1)

missing_evidence = [line for line, _ in tasks if " evidence: " not in line]
if missing_evidence:
    print(f"Missing evidence on checked tasks in Phase {phase}", file=sys.stderr)
    raise SystemExit(1)

raise SystemExit(0)
PY
}

run_check "ruff check ." ruff check .
run_check "mypy --strict src" mypy --strict src
run_check "pytest -q -m integrity" pytest -q -m integrity

if [[ "$mode" == "task" ]]; then
  run_check "pytest -q -m ${phase_marker}" pytest -q -m "$phase_marker"
else
  marker_expr="$(phase_marker_expr "$phase")"
  run_check "pytest -q -m ${marker_expr}" pytest -q -m "$marker_expr"
  run_check "pytest -q" pytest -q
fi

if (( phase >= 2 )); then
  run_check "mi-eval smoke --replay" mi-eval smoke --replay
fi

if [[ "$mode" == "exit" ]]; then
  run_check "phase ${phase} tasks ticked with evidence" check_progress_phase_exit
fi

if [[ $gate_failed -eq 0 ]]; then
  echo "GATE PASS"
  exit 0
fi

echo "GATE FAIL"
exit 1
