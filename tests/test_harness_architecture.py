"""Architecture-fitness sensors: module boundaries and runtime invariants that can be
checked structurally (the security gate emits no event, so it is checked here)."""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
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


GUIDE_DOCS = [
    Path("AGENTS.md"),
    *sorted(Path("docs/harness/skills").glob("*.md")),
    *sorted(Path("docs/runbooks").glob("*.md")),
]
_PATH_EXTENSIONS = (".py", ".md", ".json", ".jsonl", ".yml", ".yaml", ".toml", ".html", ".sh")
# Written at runtime and gitignored, so absent from a fresh clone (CI).
RUNTIME_WRITTEN = ("state/", "target/app/app.log", "target/app/data/")


def _guide_references(text: str) -> list[tuple[str, str | None]]:
    """Backticked repo paths, optionally `path::Symbol.attr`, in a guide document."""
    refs: list[tuple[str, str | None]] = []
    for span in re.findall(r"`([^`\s]+)`", text):
        path, _, symbol = span.partition("::")
        if "<" in path or path.startswith(("http", "/", *RUNTIME_WRITTEN)):
            continue  # placeholders, URLs, container/URL paths, runtime-written files
        if "/" not in path and not path.endswith(_PATH_EXTENSIONS):
            continue
        if "*" in path and "/" not in path:
            continue
        refs.append((path, symbol or None))
    return refs


def _resolve_symbol(path: Path, symbol: str) -> bool:
    body: list[ast.stmt] = ast.parse(path.read_text(encoding="utf-8")).body
    for part in symbol.split("."):
        found: ast.stmt | None = None
        for node in body:
            names: list[str] = []
            if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                names = [node.name]
            elif isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = [node.target.id]
            if part in names:
                found = node
                break
        if found is None:
            return False
        body = found.body if isinstance(found, ast.ClassDef) else []
    return True


def test_agent_guides_reference_existing_code() -> None:
    paths = 0
    symbols = 0
    for doc in GUIDE_DOCS:
        for raw_path, symbol in _guide_references(doc.read_text(encoding="utf-8")):
            path = Path(raw_path)
            if "*" in raw_path:
                assert list(Path().glob(raw_path)), f"{doc}: `{raw_path}` matches nothing"
                continue
            assert path.exists(), f"{doc}: `{raw_path}` does not exist"
            paths += 1
            if symbol is not None:
                assert path.suffix == ".py", f"{doc}: symbol on non-Python `{raw_path}`"
                assert _resolve_symbol(path, symbol), f"{doc}: `{raw_path}::{symbol}` not found"
                symbols += 1
    # Guard against the extractor silently matching nothing.
    assert paths >= 80 and symbols >= 25, (paths, symbols)


def test_runtime_written_exemptions_are_gitignored() -> None:
    # The exemption must not hide a genuinely missing tracked file.
    probes = ["state/events.jsonl", "target/app/app.log", "target/app/data/app.db"]
    assert all(any(p.startswith(prefix) for prefix in RUNTIME_WRITTEN) for p in probes)
    git = shutil.which("git")
    assert git is not None
    # Fixed git binary and repo-relative probe paths; no untrusted input.
    result = subprocess.run(  # noqa: S603
        [git, "check-ignore", *probes], capture_output=True, text=True, check=False
    )
    assert sorted(result.stdout.split()) == sorted(probes), result.stdout


def test_guide_reference_extractor_handles_forms() -> None:
    refs = _guide_references(
        "`src/a.py::B.c` `pyproject.toml` `harness/` `state/skills.md` `<name>.schema.v2.json` "
        "`workflow.step` `make check` `*.schema.v1.json` `S101` `/target/app/app.log` "
        "`/health/deep` `MI_LLM_*` `target/app/app.log` `target/app/data/app.db`"
    )
    assert refs == [("src/a.py", "B.c"), ("pyproject.toml", None), ("harness/", None)]
    assert _resolve_symbol(EXECUTOR, "ToolExecutor.security_gate")
    assert _resolve_symbol(Path("harness/mappings/runtime.py"), "README_STEP")
    assert not _resolve_symbol(EXECUTOR, "ToolExecutor.no_such_method")


def _gate_operations() -> set[str]:
    gate = _function(EXECUTOR, "security_gate")
    for node in ast.walk(gate):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "allowed_operations" for t in node.targets)
            and isinstance(node.value, ast.Set)
        ):
            return {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    raise AssertionError("allowed_operations set not found in security_gate")


def test_operation_allowlist_is_consistent() -> None:
    operations = _gate_operations()
    assert operations == {"request_backup_restore", "restart_service", "run_sql", "replace_file"}

    # Any string collection naming two or more operations is a copy of the allowlist.
    copies: dict[str, int] = {}
    for root in ("src", "tests", "scripts", "harness"):
        for path in sorted(Path(root).rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Set | ast.List | ast.Tuple) or not node.elts:
                    continue
                if not all(
                    isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts
                ):
                    continue
                values = {e.value for e in node.elts if isinstance(e, ast.Constant)}
                if len(values & operations) >= 2:
                    assert values == operations, f"{path}:{node.lineno} {sorted(values)}"
                    copies[path.as_posix()] = copies.get(path.as_posix(), 0) + 1
    assert copies == {
        "src/mi_agent/tools/executor.py": 1,
        "src/mi_agent/llm/client.py": 3,  # two HOW schema enums + allowed_operations
        "tests/harness_scenarios.py": 1,
        "tests/test_live_llm_integration.py": 1,
        "tests/test_harness_architecture.py": 1,  # the pinned expectation above
    }

    safety_line = next(
        line
        for line in Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("- Allowed dynamic operations are explicit")
    )
    assert set(re.findall(r"`([a-z_]+)`", safety_line)) == operations
