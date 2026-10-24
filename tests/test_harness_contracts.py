from __future__ import annotations

import ast
import itertools
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from harness.contracts import CONTRACT_DIR, CONTRACT_NAMES, contract_errors, load_schema
from harness.mappings.runtime import README_STEP

ENGINE_PATH = Path("src/mi_agent/agent/engine.py")
EXECUTOR_PATH = Path("src/mi_agent/tools/executor.py")


class _LiteralResolver:
    """Resolve the set of string values an expression in runtime source can take.

    Anything it cannot resolve fails the test, so new dynamic step/status forms in
    the runtime force a deliberate contract update instead of slipping through.
    """

    def __init__(self, *paths: Path) -> None:
        self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self.ambiguous: set[str] = set()
        self.calls: list[tuple[ast.Call, ast.FunctionDef | ast.AsyncFunctionDef]] = []
        defined: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    defined.append(node)
                    if node.name in self.functions:
                        self.ambiguous.add(node.name)
                    self.functions[node.name] = node
        for func in defined:
            for node in ast.walk(func):
                if isinstance(node, ast.Call):
                    self.calls.append((node, func))
        for name in self.ambiguous:
            del self.functions[name]

    def calls_to(self, name: str) -> list[tuple[ast.Call, ast.FunctionDef | ast.AsyncFunctionDef]]:
        return [
            (call, func)
            for call, func in self.calls
            if isinstance(call.func, ast.Attribute) and call.func.attr == name
        ]

    def argument(self, func_name: str, call: ast.Call, param: str) -> ast.expr | None:
        for keyword in call.keywords:
            if keyword.arg == param:
                return keyword.value
        target = self.functions[func_name]
        positional = [arg.arg for arg in target.args.args if arg.arg != "self"]
        if param in positional and positional.index(param) < len(call.args):
            return call.args[positional.index(param)]
        return None

    def param_default(
        self, func: ast.FunctionDef | ast.AsyncFunctionDef, param: str
    ) -> ast.expr | None:
        args = func.args
        for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
            if arg.arg == param and default is not None:
                return default
        offset = len(args.args) - len(args.defaults)
        for index, arg in enumerate(args.args):
            if arg.arg == param and index >= offset:
                return args.defaults[index - offset]
        return None

    def rag_meta_statuses(self) -> set[str]:
        func = self.functions["retrieve_rag_chunks"]
        values: set[str] = set()
        for node in ast.walk(func):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if isinstance(key, ast.Constant) and key.value == "status":
                        values |= self.resolve(value, func)
        return values

    def resolve(self, node: ast.expr, func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.IfExp):
            return self.resolve(node.body, func) | self.resolve(node.orelse, func)
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            return set().union(*(self.resolve(value, func) for value in node.values))
        if isinstance(node, ast.JoinedStr):
            parts: list[set[str]] = []
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    parts.append(self.resolve(value.value, func))
                else:
                    parts.append(self.resolve(value, func))
            return {"".join(combo) for combo in itertools.product(*parts)}
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "str" and len(node.args) == 1:
                return self.resolve(node.args[0], func)
            if isinstance(node.func, ast.Attribute):
                if (
                    node.func.attr == "get"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "status"
                ):
                    return self.rag_meta_statuses()
                assert node.func.attr not in self.ambiguous, f"ambiguous {node.func.attr}"
                if node.func.attr in self.functions:
                    target = self.functions[node.func.attr]
                    returns = [n for n in ast.walk(target) if isinstance(n, ast.Return)]
                    return set().union(
                        *(self.resolve(r.value, target) for r in returns if r.value is not None)
                    )
        if isinstance(node, ast.Name):
            assigned = [
                stmt.value
                for stmt in ast.walk(func)
                if isinstance(stmt, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == node.id for t in stmt.targets)
            ]
            if assigned:
                return set().union(*(self.resolve(value, func) for value in assigned))
            params = [a.arg for a in [*func.args.args, *func.args.kwonlyargs]]
            if node.id in params:
                values: set[str] = set()
                default = self.param_default(func, node.id)
                if default is not None:
                    values |= self.resolve(default, func)
                for call, caller in self.calls_to(func.name):
                    arg = self.argument(func.name, call, node.id)
                    if arg is not None:
                        values |= self.resolve(arg, caller)
                assert values, f"no values for parameter {func.name}.{node.id}"
                return values
        raise AssertionError(
            f"unresolved expression in {func.name} at line {node.lineno}: {ast.unparse(node)}"
        )


def _runtime_workflow_literals() -> dict[str, set[str]]:
    resolver = _LiteralResolver(ENGINE_PATH, EXECUTOR_PATH)
    publish = resolver.functions["_publish_workflow_step"]
    found: dict[str, set[str]] = {"step": set(), "status": set(), "path_mode": set()}
    calls = resolver.calls_to("_publish_workflow_step")
    assert calls, "runtime no longer publishes workflow.step events"
    for call, caller in calls:
        for field in found:
            value = resolver.argument("_publish_workflow_step", call, field)
            if value is None:
                default = resolver.param_default(publish, field)
                assert default is not None, f"_publish_workflow_step call omits {field}"
                found[field] |= resolver.resolve(default, publish)
            else:
                found[field] |= resolver.resolve(value, caller)
    return found


def test_contract_set_is_exactly_the_documented_contracts() -> None:
    files = {path.name for path in CONTRACT_DIR.glob("*.schema.v1.json")}
    assert files == {f"{name}.schema.v1.json" for name in CONTRACT_NAMES}


@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_contract_schema_is_valid_draft_2020_12(name: str) -> None:
    schema = load_schema(name)
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == f"mi-agent/harness/contracts/{name}.schema.v1.json"
    assert schema["type"] == "object"
    assert "schema_version" in schema["required"]
    assert schema["properties"]["schema_version"] == {"const": "v1"}


def test_contract_ids_are_unique() -> None:
    ids = [load_schema(name)["$id"] for name in CONTRACT_NAMES]
    assert len(ids) == len(set(ids))


def test_workflow_step_schema_matches_runtime_grammar_exactly() -> None:
    schema = load_schema("workflow_step")["properties"]
    runtime = _runtime_workflow_literals()

    assert runtime["step"] == set(schema["step"]["enum"])
    assert runtime["status"] == set(schema["status"]["enum"])
    assert runtime["path_mode"] == set(schema["path_mode"]["enum"])


def test_runtime_grammar_includes_dynamic_rag_steps_and_conditional_statuses() -> None:
    # Regression guard for the literal extraction itself: these values only appear
    # through f-strings, variables, or the else-branch of conditional expressions.
    runtime = _runtime_workflow_literals()
    assert {"rag.symptom", "rag.repair"} <= runtime["step"]
    assert {"error", "unhealthy", "unusable", "failed", "empty", "skipped"} <= runtime["status"]
    assert {"rejected", "failure"} <= runtime["status"]


def test_readme_step_mapping_covers_every_step_and_loop_position() -> None:
    schema_steps = set(load_schema("workflow_step")["properties"]["step"]["enum"])
    assert set(README_STEP) == schema_steps
    numbered = {value for value in README_STEP.values() if value is not None}
    assert numbered == set(range(1, 17))


def test_decision_trace_accepts_unusable_outcome_with_usable_jev_payload() -> None:
    record = {
        "schema_version": "v1",
        "ts": 1.0,
        "incident_id": "inc-1",
        "attempt": 1,
        "source": "full_loop",
        "outcome": "unusable",
        "reason": "selected_candidate_not_found",
        "candidates_origin": "jev_evaluated",
        "candidates": [{"name": "x", "operation": "restart_container", "params": {}}],
        "evaluated_count": 1,
        "selected_candidate": None,
        "decision": {"usable": True, "reason": "usable"},
        "action_key": None,
        "class_id": None,
        "source_event": "decision.unusable",
    }
    assert contract_errors(record, "decision_trace") == []


def test_decision_trace_rejects_selected_outcome_without_selection() -> None:
    record = {
        "schema_version": "v1",
        "ts": 1.0,
        "incident_id": "inc-1",
        "attempt": 1,
        "source": "full_loop",
        "outcome": "selected",
        "reason": None,
        "candidates_origin": "how_proposal",
        "candidates": [],
        "evaluated_count": 0,
        "selected_candidate": None,
        "decision": {"usable": False},
        "action_key": None,
        "class_id": None,
        "source_event": "planning.how",
    }
    errors = contract_errors(record, "decision_trace")
    assert any(error.startswith("selected_candidate") for error in errors)
    assert any(error.startswith("decision/usable") for error in errors)


def test_contract_files_are_canonical_json() -> None:
    for path in CONTRACT_DIR.glob("*.schema.v1.json"):
        json.loads(path.read_text(encoding="utf-8"))
