from __future__ import annotations

from pathlib import Path


def test_codeowners_covers_risky_paths_and_process_controls() -> None:
    text = Path(".github/CODEOWNERS").read_text(encoding="utf-8")

    required_entries = [
        "/src/mi_agent/agent/engine.py @aalricc",
        "/src/mi_agent/tools/executor.py @aalricc",
        "/src/mi_agent/tools/schemas.py @aalricc",
        "/src/mi_agent/tools/pathing.py @aalricc",
        "/src/mi_agent/decision/** @aalricc",
        "/src/mi_agent/llm/** @aalricc",
        "/src/mi_agent/config.py @aalricc",
        "/src/mi_agent/class_memory.py @aalricc",
        "/src/mi_agent/workflow/models.py @aalricc",
        "/harness/contracts/*.schema.v*.json @aalricc",
        "/harness/sensors/**/*.py @aalricc",
        "/harness/metrics/thresholds.json @aalricc",
        "/harness/guides/judge_rubric.md @aalricc",
        "/AGENTS.md @aalricc",
        "/docs/harness/HANDOFF.md @aalricc",
    ]

    for entry in required_entries:
        assert entry in text


def test_pr_template_has_risky_checklist_and_evidence_links() -> None:
    text = Path(".github/pull_request_template.md").read_text(encoding="utf-8")

    assert "Risky-Change Checklist (Governance)" in text
    for item in ("1.", "2.", "3.", "4.", "5.", "6.", "7."):
        assert f"- [ ] {item}" in text
    assert "Evidence links are provided" in text
    assert "Steering Loop Link" in text
    assert "harness/governance/README.md" in text


def test_governance_documents_d6_owners_and_branch_protection_policy() -> None:
    text = Path("harness/governance/README.md").read_text(encoding="utf-8")

    assert "## Role owners (D6)" in text
    assert "Runtime maintainer: `@aalricc`" in text
    assert "Harness maintainer: `@aalricc`" in text
    assert "Operator: `@aalricc`" in text

    assert "Temporary solo-owner exception (CP14/D6)" in text
    assert "## Branch protection for `main` (manual GitHub setting)" in text
    assert "status checks to pass with **`CI`** required" in text
    assert "`Selective LLM Review` advisory only" in text
    assert "Emergency bypass / rollback" in text

