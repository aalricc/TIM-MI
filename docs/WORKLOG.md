# Work log

Record of fixes and implementations. Each entry is the note that goes with a commit: what changed, why, and how it was checked. Commits are made by hand.

## 2026-10-16 — Keep the session plan menu when every cached action has failed

**Problem.** After a cached plan produced no remaining usable action, the recovery loop called `self._plan_cache.clear()`. That deleted every recommended plan for the process, so the selector no longer had that session's action menu to rank.

**Approach.** Failed actions stay in the executor's failed-action set, and reward scores stay in the selector and policy store. The plan cache is a separate menu. When that menu has no unfailed action left, the loop asks the model for more actions and appends only plans whose first step is not already cached. The selector still skips failed action keys.

**Files.** `src/mi_agent/agent/agent.py`, `tests/unit/test_phase7_plan_cache.py`

**Check.** `pytest -q tests/unit/test_phase7_plan_cache.py` passed (3 tests). `ruff check` passed on those files.

**Commit message.**

```
Keep failed-session plans in the plan cache.

Dropping the whole cache removed the action menu the selector uses for later choices.
```
