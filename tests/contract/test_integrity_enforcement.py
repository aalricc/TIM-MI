from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from mi_agent.events.bus import EventBus
from mi_agent.tools.executor import ToolExecutor

pytestmark = pytest.mark.integrity

_RUNTIME_ROOT = Path("src/mi_agent")
_KNOWN_VIOLATIONS_PATH = Path("docs/v2/KNOWN_VIOLATIONS.md")


def _runtime_py_files(*, include_eval: bool = True) -> list[Path]:
    paths = [
        path
        for path in sorted(_RUNTIME_ROOT.rglob("*.py"))
        if "__pycache__" not in path.parts
    ]
    if include_eval:
        return paths
    return [path for path in paths if "eval" not in path.parts]


def _known_violation_ids() -> set[str]:
    if not _KNOWN_VIOLATIONS_PATH.exists():
        return set()
    content = _KNOWN_VIOLATIONS_PATH.read_text(encoding="utf-8")
    return set(re.findall(r"\b(V2-\d{3})\b", content))


def _xfail_if_registered(violation_id: str, *, when: bool, message: str) -> None:
    if when and violation_id in _known_violation_ids():
        pytest.xfail(f"{violation_id}: {message}")


def test_import_boundary_blocks_solvability_oracle_imports() -> None:
    bad_imports: list[str] = []
    import_pattern = re.compile(r"\b(from|import)\s+(tests\.solvability|mi_agent\.eval\.solvers)")
    for path in _runtime_py_files():
        content = path.read_text(encoding="utf-8")
        if import_pattern.search(content):
            bad_imports.append(path.as_posix())
    assert bad_imports == []


def test_prompt_and_log_inputs_do_not_embed_harness_labels() -> None:
    suspicious: list[str] = []
    disallowed_patterns = (
        r"\bS(?:0[1-9]|1[0-8])\b",
        r"hidden[_\s-]?label",
        r"reference[_\s-]?route",
        r"oracle",
        r"tests/solvability",
    )
    for path in _runtime_py_files(include_eval=False):
        content = path.read_text(encoding="utf-8").lower()
        if any(re.search(pattern, content) for pattern in disallowed_patterns):
            suspicious.append(path.as_posix())
    assert suspicious == []


def test_runtime_source_does_not_read_docs_tree() -> None:
    offenders: list[str] = []
    for path in _runtime_py_files():
        content = path.read_text(encoding="utf-8").lower()
        if "docs/" in content or "docs\\" in content or "v2_spec.md" in content:
            offenders.append(path.as_posix())
    assert offenders == []


def test_primitive_catalog_exposes_risk_tier_and_neutral_description(test_settings) -> None:
    contracts = ToolExecutor(test_settings, EventBus()).primitive_parameter_contracts()

    banned_phrases = (
        "use when",
        "if the",
        "fixes",
        "for scenario",
        "for incident",
        "when db",
        "when database",
    )
    missing_metadata = False
    non_neutral = False
    for contract in contracts.values():
        description = contract.get("description")
        tier = contract.get("risk_tier")
        if not isinstance(description, str) or description.strip() == "":
            missing_metadata = True
            continue
        if not isinstance(tier, str) or tier.strip() == "":
            missing_metadata = True
        lowered = description.lower()
        if any(phrase in lowered for phrase in banned_phrases):
            non_neutral = True

    violation_present = missing_metadata or non_neutral
    _xfail_if_registered(
        "V2-004",
        when=violation_present,
        message="primitive catalog lacks required metadata or neutral descriptions",
    )
    assert not violation_present


def test_runtime_state_files_are_unseeded() -> None:
    policy_path = Path("state/policy_store.json")
    skills_json_path = Path("state/skills.json")
    skills_md_path = Path("state/global_knowledge/skills.md")

    seeded = False
    if policy_path.exists():
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        seeded = seeded or bool(policy.get("failures"))
    if skills_json_path.exists():
        skills = json.loads(skills_json_path.read_text(encoding="utf-8"))
        seeded = seeded or bool(skills)

    if skills_md_path.exists():
        text = skills_md_path.read_text(encoding="utf-8")
        begin = "<!-- MI_SKILLS_JSON_BEGIN -->"
        end = "<!-- MI_SKILLS_JSON_END -->"
        begin_index = text.find(begin)
        end_index = text.find(end)
        if begin_index != -1 and end_index > begin_index:
            block = text[begin_index + len(begin) : end_index].strip()
            if block.startswith("```"):
                lines = block.splitlines()
                if lines:
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                block = "\n".join(lines).strip()
            skills = json.loads(block) if block else []
            seeded = seeded or bool(skills)

    _xfail_if_registered(
        "V2-002",
        when=seeded,
        message="runtime state starts with pre-seeded policy/skill data",
    )
    assert not seeded


def test_verifier_logic_is_failure_type_agnostic() -> None:
    source = Path("src/mi_agent/verifier/verifier.py").read_text(encoding="utf-8")
    has_failure_type_branching = (
        "_inferred_failure_check" in source
        and "canonical_failure_type" in source
        and "db_file_missing" in source
    )

    _xfail_if_registered(
        "V2-001",
        when=has_failure_type_branching,
        message="verifier branches on failure type",
    )
    assert not has_failure_type_branching
