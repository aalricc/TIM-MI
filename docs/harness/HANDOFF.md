# Harness work handoff

Self-contained brief for the next agent (Codex or other) continuing the harness build.
Read in this order: `AGENTS.md` (repo rules) → this file → `docs/harness/HARNESS_GAP_PLAN.md`
(checkpoint plan, decisions, findings) → `docs/harness/HARNESS_ARCHITECTURE.md` →
`docs/harness/HARNESS_RUNTIME_MAPPING.md` (runtime events → harness records).

Approved scope document: `docs/harness/HARNESS_IMPLEMENTATION_WORKFLOW.md`. It is the
user's approved plan; change it only with explicit user approval (the retry rule was
amended once, approved, 2026-10-23).

## 1. Status (as of 2026-10-23)

Branch `harness` (pushed to `origin/harness`), 14 commits ahead of `main`, tree clean.

| Checkpoint | State | Commit(s) |
| --- | --- | --- |
| CP0a runtime fixes (gate as step 9, failure persist events, first-success skills, nested `dynamic_tool.executed`) | done | `b3ca2d5` |
| CP0b harness scaffolding (contracts, mappings, sensors, KPIs, docs, CI) | done | `b7c2061` |
| CP1 security-gate rejection = failed attempt with penalties (decision D1 = b); benchmark repair | done | `6249d9b` |
| CP2 pre-commit hook + ruff `S` security rules | done | `7566b34` |
| CP3 pip-audit CI job + weekly drift workflow; pypdf/pytest floors raised | done | `32fcd5a` |
| CP4 `AGENTS.md`, contributor skills, guide/allowlist consistency tests | done | `585f7d6` |
| CP5 runbooks, known-good backup finder, threat-model residual risks | done | `dcb86ba` |
| Pre-CP7 fixes: fresh-clone CI fix, JEV SDK pin + JEV tests in CI, `pydantic-settings>=2.15`, target deep health checks widget columns, validated backups + retention, compose class-memory default | done | `ebd3978`, `e264372`, `f6eca3b`, `d7c77be`, `0ed94ad` |
| User decisions: remove `target/last_resort`; clear `state/` (archived) | done | `5728a60` |
| User decision: re-check health before each retry; externally repaired incidents close without learning | done | `a98d4ff` |
| **CP6** scenario fixtures, event slicing, outcome checker | **next** | — |
| CP7–CP14 | not started | — |

Checks at `a98d4ff`: `make check` green (ruff, mypy `src`, mypy `harness`, 238 passed /
3 skipped in the dev-only `.venv`; 0 skipped with `.[dev,jev,rag]`).

## 2. Next: CP6 — scenario fixtures, event slicing, outcome checker

Offline only (no Docker, no credentials). One commit:
`feat(harness): scenario fixtures, event slicing, outcome checker`.

Deliverables:

1. **Contract** `harness/contracts/scenario_fixture.schema.v1.json`: `schema_version`,
   `scenario_id`, `fault` (`drop_widgets_table` | `delete_db` | `corrupt_db`),
   `repetitions`, `approved` { `operations` (allowed winning operations), `max_attempts`,
   `max_ttr_seconds` }, `source` (runbook path). Add the name to
   `harness/contracts/__init__.py::CONTRACT_NAMES`;
   `tests/test_harness_contracts.py::test_contract_set_is_exactly_the_documented_contracts`
   requires the v1 file set to equal that tuple. Update `harness/contracts/README.md`.
2. **Fixtures** (data, one JSON per fault) under a new `harness/scenarios/` directory.
   Approved operations come from the runbooks' "Approved recovery actions" tables:
   `drop_widgets_table` → `run_sql`, `request_backup_restore`; `delete_db` →
   `request_backup_restore`, `replace_file`; `corrupt_db` → `request_backup_restore`,
   `replace_file`. Use `max_attempts` 3 and `max_ttr_seconds` 300, matching the escalation
   thresholds in `docs/runbooks/stuck-incident.md`; flag them as operator-approved values
   in the PR so the user can change them.
3. **Slicer**: `python -m harness.events.slice --since/--until/--incident <log>` prints the
   matching JSONL rows **to stdout** (the caller redirects). Harness code must not write
   files (`tests/test_harness_architecture.py::test_harness_code_is_read_only`) and must
   not import `mi_agent`. Times: ISO-8601 with offset or epoch seconds.
4. **Checker**: `python -m harness.scenarios.check <fixture> <log-slice>` prints one
   `sensor_result` (`lane` = `behavior`, `stage` = `ci` or `continuous`) per fixture. For
   every incident in the slice: recovered (`incident.recovered`), attempts ≤
   `max_attempts`, TTR ≤ `max_ttr_seconds`, the recovering decision's operation (the
   `selected` `decision_trace` in the recovered attempt) is in `approved.operations`, and
   the runtime invariant sensors pass (`harness/sensors/runtime_invariants.py::run_all`).
   Exit 1 on fail. Reuse `harness/mappings/runtime.py` and `harness/metrics/kpis.py`.
5. **Tests** using `tests/harness_scenarios.py` logs: a passing fixture, each failure mode
   (not recovered, too many attempts, TTR over budget, unapproved operation, invariant
   violation), slicer boundaries, and the CLIs' exit codes. Validate outputs against the
   contracts.
6. **Docs**: flip "Approved recovery outcomes" in `harness/guides/README.md` and the
   relevant sensor rows; add CP6 "done" notes to the plan; replace the not-yet-built slice
   step in `docs/harness/BASELINE.md` § 4 with the real commands.

Then CP7 needs decision **D2** (where the live campaign runs) from the user before work
starts; see the plan's decisions table. D3–D6 block CP9, CP12, CP13, CP14.

## 3. How this work has been done (keep doing it)

- **The user commits and pushes; the agent never does.** At the end of each checkpoint,
  give exact `git add` / `git commit` commands, one logical change per commit, with file
  sets that do not overlap between commits.
- **Prove each commit is green before handing over commands.** Replay the commits in order
  in a clean worktree (`git worktree add <scratch> HEAD`, copy each commit's files in,
  run ruff, `mypy --strict src`, `mypy --strict harness`, pytest), then remove the
  worktree.
- **Pre-commit gotcha:** the hook stashes *unstaged tracked* changes but leaves
  *untracked* files in place. A new, untracked test file that belongs to a later commit
  is therefore present during earlier commits' hook runs and can fail them. Tell the user
  to move such files aside (`mv tests/x.py ~/x.py.hold`) and restore them before their
  commit. `git add -u <path>` does not stage untracked files.
- **Evidence over assertion.** Check claims against code and data; quote recorded
  incidents when describing behaviour. Mutation-check new tests (break the code, confirm
  the test fails, restore). Verify dependency floors with an exact-floor install and a
  `--resolution lowest-direct` install.
- **Python versions:** the local `.venv` is Python 3.14 with `.[dev]`; CI and Docker use
  3.12 with `.[dev,jev,rag]`. Use `uv venv --seed --python 3.12 <scratch>` (uv is at
  `~/.local/bin/uv`) for CI-parity runs. pytest's config already adds `-q`; use
  `-o addopts=""` to see the summary line.
- **Risky changes** (list in `harness/governance/README.md`) need explicit user approval.
  Ask in plain language with options and a recommendation; the user prefers short,
  simple explanations.
- **Keep docs in sync in the same commit:** catalogue status rows
  (`harness/guides/README.md`, `harness/sensors/README.md`, `harness/governance/README.md`),
  plan "done" notes, runbooks. Paths in `AGENTS.md`, `docs/harness/skills/`, and
  `docs/runbooks/` are checked by
  `tests/test_harness_architecture.py::test_agent_guides_reference_existing_code`.

## 4. Environment facts

- `state/` was cleared on 2026-10-23 so MI relearns under the harness. The pre-harness
  data (21 MB event log with 34 incidents, class store, skills, tool catalog, policy) is in
  `results/state-archive-2026-10-23.tar.gz` (gitignored). Extract to a scratch directory
  for evidence or baseline comparison; do not restore into `state/` unless the user asks.
- Docker CLI is installed but the daemon is usually not running; `/bin/dash` is available
  locally (the backup-job container's `/bin/sh`).
- JEV is a self-hosted open-source model reached through the third-party alpha SDK
  `langchain-typesafe==0.0.1a3` (pinned). The JEV server checkout (`.local/jev/jev` in
  `README.md`) is not on this machine.
- `ruff format` is not enforced in this repo; match surrounding style.

## 5. Open items not covered by a checkpoint

- The new backup job (`target/backup/backup.sh` on `python:3.12-slim`) is tested under
  `sh` and `dash` but has not run in its container yet. When Docker is up:
  `docker compose up -d --build target-app backup-job`, then
  `docker compose exec backup-job ls -lt /target/backups | head -3`.
- The CI `audit` job and the JEV tests in CI run on the next push or PR; confirm green.
  `.github/workflows/drift.yml` can only be scheduled or dispatched once it is on the
  default branch.
- Existing quirk (not fixed, user not yet asked): if an incident closes before MI's first
  attempt, `RecoveryAgent.run_iteration` applies `WorkflowEvent.STABLE`, which leaves the
  workflow state `incident_active` until the next incident.
- Open decision recorded in the plan: replace the JEV SDK with a direct `httpx` client
  (risky; needs the JEV server API).
