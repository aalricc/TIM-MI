# Skill: add or change a dynamic tool operation

A dynamic tool operation is something MI can execute in step 11 (today:
`request_backup_restore`, `restart_service`, `run_sql`, `replace_file`). Adding one widens
what an LLM-proposed, JEV-selected action can do to the target, so it is a **risky change**
(`harness/governance/README.md`): get approval and state the blast radius in the PR.

## Steps

1. **Parameter model.** Add a pydantic model in `src/mi_agent/tools/schemas.py` with tight
   bounds (`Field(min_length=…, max_length=…, ge=…, le=…)`). Every parameter the operation
   reads must be in the model.
2. **Security gate (step 9).** In `src/mi_agent/tools/executor.py::ToolExecutor.security_gate`,
   add the operation to `allowed_operations` and validate its parameters with the new
   model. Resolve every filesystem path through
   `src/mi_agent/tools/pathing.py::PathGuard.resolve_in_sandbox` (sources) or
   `src/mi_agent/tools/pathing.py::PathGuard.resolve_in_app_root` (targets). The gate must
   raise `SecurityGateError` or pydantic `ValidationError` on rejection; the engine turns
   only those two into a failed attempt.
3. **Execution (step 11).** Add a branch to
   `src/mi_agent/tools/executor.py::ToolExecutor._execute_operation` that parses the
   parameters with the new model (as the existing branches do) and awaits a new private
   handler. The handler returns, rather than raises, for expected failures: a dict with
   `"ok": False` and an `"error"` string, or on success `"ok": True` plus
   `"action": "<operation>"` and any result details. The engine reads `ok`/`error` and
   nests the whole dict under `result` in `dynamic_tool.executed`.
4. **LLM HOW contract.** Add the operation to both `operation` enums in
   `src/mi_agent/llm/client.py::OpenAILLMClient._how_schema` and to `allowed_operations`
   in `src/mi_agent/llm/client.py::OpenAILLMClient._how_user_payload`. If it is a fallback
   like `restart_service`, add a matching rule. Prompt changes are risky changes too.
5. **Candidate normalisation (optional).** If LLMs use parameter aliases or placeholders,
   add a branch to `src/mi_agent/agent/engine.py::RecoveryAgent._normalize_candidate`.
   Without a branch the candidate passes through unchanged.
6. **Class-memory action key.** Add branches to
   `src/mi_agent/class_memory.py::_canonical_params_for_action` and
   `src/mi_agent/class_memory.py::_target_kind` so equivalent parameter sets get the same
   action key (dedupe and fast-path stats depend on it). Changing these for an *existing*
   operation changes its action keys and orphans learned stats in `state/class_store.json`:
   treat that as a migration and say so in the PR.
7. **Other copies of the allowlist.** Update `tests/harness_scenarios.py::GATE_ALLOWLIST`,
   the set in `tests/test_live_llm_integration.py`, and the Safety model line in
   `docs/ARCHITECTURE.md`.
   `tests/test_harness_architecture.py::test_operation_allowlist_is_consistent` fails until
   every copy matches the gate.
8. **Tests.** In `tests/test_tools_executor.py`: one test that the gate accepts valid
   parameters, one per rejection (unknown parameter shape, path escape), and one for the
   execution result (`ok` true and false). Add a class-memory test if you added a
   canonical-key branch.
9. **Runbooks.** If a fault runbook should recommend the operation, update it.

## Done when

```bash
make check
make audit   # if a dependency was added for the handler
```

and the PR states: risky-change category, blast radius, and whether any existing action
keys changed.
