# Harness work handoff

Self-contained brief for the next agent (Codex or other) continuing the harness build.
Read in this order: `AGENTS.md` (repo rules) → this file → `docs/harness/HARNESS_GAP_PLAN.md`
(checkpoint plan, decisions, findings) → `docs/harness/HARNESS_ARCHITECTURE.md` →
`docs/harness/HARNESS_RUNTIME_MAPPING.md` (runtime events → harness records).

Approved scope document: `docs/harness/HARNESS_IMPLEMENTATION_WORKFLOW.md`. It is the
user's approved plan; change it only with explicit user approval (the retry rule was
amended once, approved, 2026-10-23).

## 1. Status (as of 2026-10-26)

Branch `main` (synced to `origin/main`), tree clean.

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
| CP6 scenario fixtures, event slicing, outcome checker | done | `d5ab37b` |
| CP7 live E2E fault-injection campaign (manual `workflow_dispatch` on self-hosted runner, D2=(c)) | done | `57a86d9` |
| CP8 baseline campaign + calibration | done | `a41e770`, `8c7fd87` |
| CP9 continuous KPI + invariant surveillance | done | `faf70be`, `1f3ee12` |
| CP10 runtime budget sensors (step latency, token budget, JEV call budget, telemetry) | done | `44aa4aa`, `9da180d`, `bc07b51` |
| CP11 decision-log sampling for human review | done | this commit |
| CP12 LLM-as-judge on diagnosis/action quality | done | this commit |
| **CP13** selective LLM code / architecture review (needs D5) | **next** | — |
| CP14 | not started | — |

`make check` green at CP12 (ruff, mypy `src`, mypy `harness`, pytest; 3 skipped in the
dev-only `.venv`, 0 with `.[dev,jev,rag,trace]`).

## 2. Next: CP13 — selective LLM code / architecture review (blocked by D5)

CP12 is complete once this commit lands (rubric approved, judge + calibration tools,
contracts/tests/docs synced).

Before implementation, confirm D5 in `docs/harness/HARNESS_GAP_PLAN.md`:
- Which agent/provider runs selective PR review on risky paths.

Then implement CP13 exactly as specified in the plan:
1. Trigger review only on risky paths from governance.
2. Post advisory review comments (never merge-blocking).
3. Add the log-quality reflection prompt for failed CP7 campaigns.

Commit target after D5 is decided:
`ci(harness): selective LLM review on risky paths`

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
  3.12 with `.[dev,jev,rag,trace]`. Use `uv venv --seed --python 3.12 <scratch>` (uv is at
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
