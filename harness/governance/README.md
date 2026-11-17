# Harness Governance

Where checks run, what needs human approval, how harness changes are promoted, and how
runtime-written artifacts are governed. Governance never relaxes a runtime invariant.

## Role owners (D6)

- Runtime maintainer: `@aalricc`
- Harness maintainer: `@aalricc`
- Operator: `@aalricc`

## Quality placement (keep quality left)

| Stage | Runs | Command | Status |
| --- | --- | --- | --- |
| **Pre-commit / every change** | lint, type checks (runtime + harness), unit tests, contract validation, structural architecture checks, scripted recovery regression | `.pre-commit-config.yaml` (same commands as `make check`) | wired; each clone runs `pre-commit install` once |
| | basic security scan | ruff `S` (flake8-bandit) rules, part of `ruff check .` | wired (pre-commit + CI) |
| **CI/CD after integration** | everything above again | `.github/workflows/ci.yml` (`ruff`, `mypy --strict src`, `mypy --strict harness`, `pytest -q`) | wired |
| | dependency vulnerability scan | `.github/workflows/ci.yml` job `audit` (`pip-audit` over `.[dev,jev,rag,trace]`) | wired |
| | full E2E fault-injection recovery regression | `README.md` quick live recovery test | manual |
| | costlier inferential review (LLM code / architecture review) | `.github/workflows/pr-gates.yml` `agent-review`: independent reviewer agent, required, fail-closed, every PR (replaced the advisory selective review, 2026-11-17) | wired |
| | required approval for risky changes | PR review (see below) | manual |
| **Continuous (outside change lifecycle)** | dependency / security drift scans | `.github/workflows/drift.yml` (weekly + manual; fresh resolution, `pip-audit` + ruff `S`; opens/updates one tracking issue) | wired |
| | runtime KPI surveillance (recovery rate, TTR, JEV request errors vs `harness/metrics/thresholds.json`) | compose `harness-watch` service: `python -m harness.watch` every 5 min over 24 h → `results/harness/sensor_results.jsonl`, dashboard Surveillance panel | wired |
| | invariant surveillance on live logs | same `harness-watch` run (all invariant sensors, `stage=continuous`) | wired |
| | periodic decision-log sampling and review | weekly: `python -m harness.review.sample --n 20 > results/reviews/packets-$(date +%F).md`, reviewers save JSON verdicts in `results/reviews/` (`review_verdict`), run `python -m harness.review.judge --events state/events.jsonl --n 20 > results/reviews/judge-$(date +%F).jsonl`, then `python -m harness.review.calibrate results/reviews/ results/reviews/judge-$(date +%F).jsonl > results/reviews/calibration-$(date +%F).json`; optional trusted mode rerun: `python -m harness.review.judge --events state/events.jsonl --n 20 --calibration results/reviews/calibration-$(date +%F).json` | manual |

## Changes that require explicit human approval

A change is **risky** and needs sign-off from a reviewer other than the author (or the
agent that drafted it) when it touches any of:

1. Runtime control flow in `src/mi_agent/agent/engine.py` (step order, retry, fast-path
   gates, persistence).
2. The security gate or operation allowlist (`ToolExecutor.security_gate`,
   `src/mi_agent/tools/schemas.py`, `src/mi_agent/tools/pathing.py`).
3. JEV integration or thresholds (`src/mi_agent/decision/`, `MI_JEV_*`, `MI_CLASSMEM_*`).
4. LLM prompts or output parsing (`src/mi_agent/llm/`).
5. A harness contract major version, a `workflow_step` enum, or any sensor's pass/fail rule.
6. Hand edits to runtime-written state (`state/skills.md`, `state/class_store.json`,
   `state/policy_store.json`, `state/tool_catalog.json`).
7. Judge rubric changes that alter grading criteria or threshold semantics
   (`harness/guides/judge_rubric.md`).

Non-risky (normal review): harness docs, new sensors that only add checks, KPI reporting,
test fixtures that do not weaken assertions.

Temporary solo-owner exception (CP14/D6): while all three role owners above are the same
person (`@aalricc`), self-approval is allowed for risky changes. Remove this exception once
another maintainer role owner is assigned.

## Agent PR pipeline and branch protection for `main` (ruleset `main-protection`)

Industry pattern: the agent never merges on its own judgement; branch rules merge a PR
once every required check is green, and an independent reviewer agent is one of those
checks. Risk tiering keeps a human on risky changes (SLSA treats a bot as not a trusted
person, so a bot review alone never stands in for owner sign-off).

```
prompt → author agent: branch → commits (pre-commit = make check) → push
       → PR (template, risky items named) → gh pr merge --auto --rebase
PR checks, all required:
  lint-type-test · dependency-audit · docker-build-smoke      (.github/workflows/ci.yml)
  agent-review   independent reviewer agent, fail-closed       (.github/workflows/pr-gates.yml)
  risk-gate      CODEOWNERS path touched → needs owner sign-off (.github/workflows/pr-gates.yml)
all green → GitHub auto-merges → CD on main: images to GHCR, site to Vercel (cd.yml)
```

Gate rules (`harness/review/gate.py`, tested in `tests/test_harness_pr_gates.py`):

- **`agent-review`**: a separate model (configured by `REVIEW_LLM_*`, default
  `gpt-5.3-codex`; not the author's model) reviews the diff against AGENTS.md and this
  file, and returns a strict JSON verdict. It passes only on an explicit `approve` with no
  blocking finding. A missing key, model error after retries, unparseable verdict, empty
  file list or a diff over 150k characters fails the check. PR text is passed as data;
  the reviewer is told to ignore instructions inside it.
- **`risk-gate`**: every path `.github/CODEOWNERS` assigns is risky, including the
  pipeline itself (`.github/**`, `harness/review/gate.py`, `harness/governance/**`,
  `.claude/settings.json`). A risky PR needs owner sign-off on the current head: an
  APPROVED review on the head commit from a code owner who is not the author, or the
  `owner-approved` label added by a code owner. The workflow deletes that label on every
  push. Owner sign-off also overrides a wrong `agent-review` block.
- **Trusted execution**: `pr-gates.yml` runs on `pull_request_target`, so it always runs
  the base branch's copy of the workflow and gate code; PR content is fetched through the
  API as data and never checked out or executed. Results are commit statuses on the head
  commit; a run that dies leaves them `pending` (blocks). Every action is pinned to a
  commit SHA.
- **Agent limits** (`.claude/settings.json` deny rules, AGENTS.md): the agent never adds
  `owner-approved`, approves PRs, merges with `--admin`, or edits rulesets/branch
  protection.

Ruleset `main-protection` (Settings → Rules), owner-applied:

1. Require a pull request before merging; approvals **0** (owner sign-off is enforced by
   **`risk-gate`**, because GitHub never lets an author approve their own PR and on this
   solo repo the agent pushes with the owner's account).
2. Require conversation resolution before merging.
3. Require status checks, branch up to date: **`lint-type-test`**, **`dependency-audit`**,
   **`docker-build-smoke`**, **`agent-review`**, **`risk-gate`**.
4. Block force pushes and branch deletion. Repository setting: allow auto-merge.
5. Bypass: the repository owner only (emergency use, see below).

When a second maintainer joins: give the agent its own GitHub identity (machine user or
GitHub App) so PR authorship and owner sign-off are different accounts, and raise
approvals to 1 with Code Owners review.

Apply (owner runs this; ruleset id from `gh api repos/<owner>/<repo>/rulesets`):

```bash
gh api repos/<owner>/<repo>/rulesets/<id> > ruleset.json
jq '{name,target,enforcement,bypass_actors,conditions,
     rules: (.rules | map(
       if .type == "required_status_checks" then .parameters.required_status_checks =
         [{"context":"lint-type-test"},{"context":"dependency-audit"},
          {"context":"docker-build-smoke"},{"context":"agent-review"},
          {"context":"risk-gate"}]
       elif .type == "pull_request" then .parameters.required_approving_review_count = 0
         | .parameters.require_code_owner_review = false
       else . end))}' ruleset.json > ruleset.new.json
gh api -X PUT repos/<owner>/<repo>/rulesets/<id> --input ruleset.new.json
gh api -X PATCH repos/<owner>/<repo> -F allow_auto_merge=true
```

Verify:

```bash
gh api repos/<owner>/<repo>/rulesets/<id> --jq '.rules[] | select(.type ==
  "required_status_checks" or .type == "pull_request") | .parameters'
```

Emergency bypass / rollback (production-impacting hotfix only):

1. A maintainer-role owner may temporarily bypass protections (currently `@aalricc`).
2. Record reason, incident link, and timestamp in PR and `docs/WORKLOG.md`.
3. Merge the minimal hotfix.
4. Re-enable standard protections immediately after merge.
5. Open a follow-up PR to restore normal review flow and capture lessons learned.

## Dependency vulnerabilities

When `pip-audit` fails (CI job `audit` or the weekly drift issue):

1. Raise the affected floor in `pyproject.toml` to the first fixed version (and the cap
   if the fix needs a new major). Check that the new floor is compatible with its
   dependents (e.g. pytest 9 needs pytest-asyncio ≥ 1.3).
2. Verify on Python 3.12 (CI and Docker version) with `.[dev,jev,rag,trace]`: `make check`,
   `make audit`. For a changed floor, also install that exact floor version and re-run
   the tests.
3. A floor change to a runtime dependency (base, `jev`, `rag`) is reviewed for behaviour
   change. `rag` pins (`pypdf`, `pinecone`) affect ingestion output: re-run
   `scripts/rag_eval.py` after the next re-ingest.
4. Ignoring an advisory is a last resort: add `--ignore-vuln <ID>` to the workflow with a
   comment giving the reason and a review date, and keep the tracking issue open. This
   is a risky change (approval required).

## Runtime-written artifacts

| Artifact | Written by | Rule |
| --- | --- | --- |
| `state/tool_catalog.json` | step 13, success only | only the verified winning tool is persisted |
| `state/policy_store.json` | step 14 (+1) and failed attempts (−1, except `jev_request_error`) | no manual edits without approval |
| `state/skills.md` | step 15 | class sections are replaced in place per class; legacy blocks append; review before reuse in another environment |
| `state/class_store.json` | step 16 and failed outcomes | schema version guarded by `class_memory._SCHEMA_VERSION`; corrupt files are moved aside by the runtime |
| `state/events.jsonl` | `EventLog` | append-only; source of truth for sensors and KPIs |

Rollback: restore the previous file from backup or delete the learned entry, then record
the reason in `docs/WORKLOG.md`. A rollback of `skills.md` or `class_store.json` is a risky
change (item 6).

## Contract evolution

Additive, backward-compatible contract changes stay in v1. Breaking changes add a
`*.schema.v2.json` alongside v1; v1 is removed only when no consumer reads it. A runtime
change that alters the `workflow.step` grammar must update the contract in the same change
(enforced by `tests/test_harness_contracts.py`).

## Steering loop (S1–S6)

Run whenever an issue occurs more than once.

| Step | Actor | Action | Inputs / outputs |
| --- | --- | --- | --- |
| S1 Failure signals | MI / harness | Collect event log, decision log, metrics, failed regressions | `state/events.jsonl`, `decision_trace`, KPI JSON, failing sensors/tests |
| S2 Triage patterns | human | Group repeated failures, find root causes | group by `incident_signature.failure_type` / `class_id`, `decision_trace.reason`, sensor violations |
| S3 Draft a better control | LLM / coding agent | Propose a guide, sensor, test, or skill change | a PR that changes one guide/sensor/test/skill |
| S4 Validate | MI / CI | Run tests, contract, security, regression checks | `make check` locally, CI green |
| S5 Review & approve | human | Sign-off, mandatory for risky changes (list above), using `docs/harness/skills/review-checklist.md` | PR approval |
| S6 Promote | CI/CD | Merge and deploy runtime and harness together | merge to `main` |

A repeated issue is closed only when a guide or sensor exists that would have prevented
or caught it.
