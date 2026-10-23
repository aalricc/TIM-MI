"""Architecture-fitness sensors: module boundaries and runtime invariants that can be
checked structurally (the security gate emits no event, so it is checked here)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path("src/mi_agent")
HARNESS = Path("harness")
ENGINE = SRC / "agent" / "engine.py"
EXECUTOR = SRC / "tools" / "executor.py"
README = Path("README.md")
MAPPING_DOC = Path("docs/harness/HARNESS_RUNTIME_MAPPING.md")


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def _function(path: Path, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    ]
    assert len(matches) == 1, f"expected one {name} in {path}"
    return matches[0]


def _method_calls(node: ast.AST, attr: str) -> list[ast.Call]:
    return sorted(
        (
            call
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == attr
        ),
        key=lambda call: (call.lineno, call.col_offset),
    )


def test_runtime_does_not_import_harness() -> None:
    for path in SRC.rglob("*.py"):
        offenders = {m for m in _imported_modules(path) if m.split(".")[0] == "harness"}
        assert not offenders, f"{path} imports harness: {offenders}"


def test_harness_does_not_import_runtime() -> None:
    for path in HARNESS.rglob("*.py"):
        offenders = {m for m in _imported_modules(path) if m.split(".")[0] == "mi_agent"}
        assert not offenders, f"{path} imports runtime: {offenders}"


def test_harness_code_is_read_only() -> None:
    write_methods = {"write_text", "write_bytes", "unlink", "rename", "replace", "mkdir", "touch"}
    for path in HARNESS.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                assert node.func.attr not in write_methods, f"{path}:{node.lineno} writes files"
            if isinstance(node.func, ast.Name) and node.func.id == "open":
                mode = node.args[1] if len(node.args) > 1 else None
                for keyword in node.keywords:
                    if keyword.arg == "mode":
                        mode = keyword.value
                assert isinstance(mode, ast.Constant) and set(str(mode.value)) <= {"r", "b"}, (
                    f"{path}:{node.lineno} opens a file for writing"
                )


def test_security_gate_runs_before_registration_and_execution() -> None:
    # README step 9 (gate) before step 10 (register) in the engine.
    selected = _function(ENGINE, "_execute_selected_action")
    engine_gate = _method_calls(selected, "security_gate")
    registered = _method_calls(selected, "register_temporary_dynamic_tool")
    assert len(engine_gate) == 1 and len(registered) == 1
    assert engine_gate[0].lineno < registered[0].lineno

    # The executor re-checks at execution time, so no caller can run an ungated spec.
    execute = _function(EXECUTOR, "execute_dynamic_tool")
    gate = _method_calls(execute, "security_gate")
    operation = _method_calls(execute, "_execute_operation")
    assert len(gate) == 1 and len(operation) == 1
    assert gate[0].lineno < operation[0].lineno

    tree = ast.parse(EXECUTOR.read_text(encoding="utf-8"))
    callers = {
        func.name
        for func in ast.walk(tree)
        if isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef)
        and _method_calls(func, "_execute_operation")
    }
    assert callers == {"execute_dynamic_tool"}


def test_engine_executes_and_verifies_once_per_attempt_structurally() -> None:
    tree = ast.parse(ENGINE.read_text(encoding="utf-8"))
    executions = _method_calls(tree, "execute_dynamic_tool")
    verifications = _method_calls(tree, "verify")
    assert len(executions) == 1, "exactly one dynamic execution call site"
    assert len(verifications) == 1, "exactly one verifier call site"

    selected = _function(ENGINE, "_execute_selected_action")
    registered = _method_calls(selected, "register_temporary_dynamic_tool")
    executed = _method_calls(selected, "execute_dynamic_tool")
    verified = _method_calls(selected, "verify")
    assert len(registered) == len(executed) == len(verified) == 1
    assert registered[0].lineno < executed[0].lineno < verified[0].lineno


def test_jev_is_the_only_path_to_execution() -> None:
    attempt = _function(ENGINE, "_attempt_recovery")
    selects = _method_calls(attempt, "_select_candidate_with_jev")
    executes = _method_calls(attempt, "_execute_selected_action")
    assert len(selects) == len(executes) == 2  # fast path + full loop
    for select, execute in zip(selects, executes, strict=True):
        assert select.lineno < execute.lineno

    tree = ast.parse(ENGINE.read_text(encoding="utf-8"))
    callers = {
        func.name
        for func in ast.walk(tree)
        if isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef)
        and _method_calls(func, "_execute_selected_action")
    }
    assert callers == {"_attempt_recovery"}


def test_readme_loop_has_sixteen_steps_and_step5_retry_rule() -> None:
    text = README.read_text(encoding="utf-8")
    loop = text.split("The runtime enforces a strict loop:", 1)[1].split("## ", 1)[0]
    numbers = [int(n) for n in re.findall(r"^(\d+)\. ", loop, flags=re.MULTILINE)]
    assert numbers == list(range(1, 17))
    assert "loop back to step 5 (no hard cap)" in loop


def test_mapping_doc_covers_every_readme_step() -> None:
    text = MAPPING_DOC.read_text(encoding="utf-8")
    rows = [int(n) for n in re.findall(r"^\| (\d+) \|", text, flags=re.MULTILINE)]
    assert rows == list(range(1, 17))
