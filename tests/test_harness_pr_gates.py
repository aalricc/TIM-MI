from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from typing import Any

import httpx
import pytest

from harness.review.gate import (
    MAX_DIFF_CHARS,
    OWNER_LABEL,
    REVIEW_SYSTEM_PROMPT,
    ReviewerConfig,
    SignOff,
    active_owners,
    agent_review,
    judge_verdict,
    main,
    owner_sign_off,
    parse_codeowners,
    risk_gate,
    risky_paths,
)

CODEOWNERS = parse_codeowners(Path(".github/CODEOWNERS").read_text(encoding="utf-8"))
PERMS = {"aalricc": "admin"}
HEAD = "abc123"


def _pr(*, author: str = "aalricc", labels: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "title": "t",
        "body": "b",
        "user": {"login": author},
        "head": {"sha": HEAD},
        "labels": [{"name": name} for name in labels],
    }


def _labeled(actor: str, name: str = OWNER_LABEL) -> dict[str, Any]:
    return {"event": "labeled", "label": {"name": name}, "actor": {"login": actor}}


CONFIG = ReviewerConfig(endpoint="https://llm.test/v1/responses", model="m", api_key="k",
                        auth_mode="bearer")
NO_OVERRIDE = SignOff(approved=False)


# ---------------------------------------------------------------- risky paths


@pytest.mark.parametrize(
    "path",
    [
        "src/mi_agent/agent/engine.py",
        "src/mi_agent/llm/client.py",
        "src/mi_agent/decision/client.py",
        "harness/contracts/sensor_result.schema.v1.json",
        "harness/sensors/runtime_invariants.py",
        ".github/workflows/pr-gates.yml",
        ".github/workflows/ci.yml",
        ".github/CODEOWNERS",
        "harness/review/gate.py",
        "harness/governance/README.md",
        ".claude/settings.json",
        "state/skills.md",
    ],
)
def test_pipeline_and_runtime_risk_paths_need_the_owner(path: str) -> None:
    assert risky_paths([path], CODEOWNERS) == [path]


@pytest.mark.parametrize("path", ["README.md", "site/index.html", "tests/test_x.py",
                                  "docs/runbooks/delete_db.md", "dashboard/index.html"])
def test_low_risk_paths_do_not(path: str) -> None:
    assert risky_paths([path], CODEOWNERS) == []


# ---------------------------------------------------------------- risk gate


def test_low_risk_pr_passes_without_sign_off() -> None:
    result = risk_gate(changed=["README.md"], codeowners=CODEOWNERS, pr=_pr(),
                       label_events=[], reviews=[], permissions=PERMS)
    assert result["pass"] is True


def test_empty_file_list_fails_closed() -> None:
    result = risk_gate(changed=[], codeowners=CODEOWNERS, pr=_pr(), label_events=[],
                       reviews=[], permissions=PERMS)
    assert result["pass"] is False


def test_risky_pr_blocks_until_owner_label() -> None:
    changed = ["src/mi_agent/llm/client.py"]
    blocked = risk_gate(changed=changed, codeowners=CODEOWNERS, pr=_pr(), label_events=[],
                        reviews=[], permissions=PERMS)
    assert blocked["pass"] is False
    signed = risk_gate(changed=changed, codeowners=CODEOWNERS,
                       pr=_pr(labels=(OWNER_LABEL,)), label_events=[_labeled("aalricc")],
                       reviews=[], permissions=PERMS)
    assert signed["pass"] is True


def test_label_added_by_non_owner_does_not_count() -> None:
    sign_off = owner_sign_off(pr=_pr(labels=(OWNER_LABEL,)),
                              label_events=[_labeled("mallory")], reviews=[],
                              owners={"aalricc"})
    assert sign_off.approved is False


def test_label_removed_after_push_does_not_count() -> None:
    # The workflow deletes the label on every push; an old `labeled` event alone is stale.
    sign_off = owner_sign_off(pr=_pr(), label_events=[_labeled("aalricc")], reviews=[],
                              owners={"aalricc"})
    assert sign_off.approved is False


def test_owner_review_counts_only_on_head_and_not_from_author() -> None:
    owners = {"aalricc", "second"}
    stale = [{"user": {"login": "second"}, "state": "APPROVED", "commit_id": "old"}]
    assert owner_sign_off(pr=_pr(), label_events=[], reviews=stale, owners=owners).approved is False
    current = [{"user": {"login": "second"}, "state": "APPROVED", "commit_id": HEAD}]
    assert owner_sign_off(pr=_pr(), label_events=[], reviews=current, owners=owners).approved
    self_review = [{"user": {"login": "aalricc"}, "state": "APPROVED", "commit_id": HEAD}]
    assert not owner_sign_off(pr=_pr(), label_events=[], reviews=self_review,
                              owners=owners).approved


def test_later_changes_requested_supersedes_approval() -> None:
    reviews = [
        {"user": {"login": "second"}, "state": "APPROVED", "commit_id": HEAD},
        {"user": {"login": "second"}, "state": "CHANGES_REQUESTED", "commit_id": HEAD},
    ]
    assert not owner_sign_off(pr=_pr(), label_events=[], reviews=reviews,
                              owners={"second"}).approved


# ---------------------------------------------------------------- agent review


def _mock_model(monkeypatch: pytest.MonkeyPatch, verdict: Any, status: int = 200) -> list[Any]:
    seen: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        text = verdict if isinstance(verdict, str) else json.dumps(verdict)
        return httpx.Response(status, request=request,
                              json={"output": [{"content": [{"type": "output_text",
                                                             "text": text}]}]})

    real = httpx.Client

    def factory(**kwargs: Any) -> httpx.Client:
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("harness.review.gate.httpx.Client", factory)
    return seen


def _review(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {"changed": ["README.md"], "diff": "+hello", "pr": _pr(),
                            "rules": {"AGENTS.md": "rules"}, "config": CONFIG,
                            "owner_override": NO_OVERRIDE}
    args.update(overrides)
    return agent_review(**args)


def test_review_passes_on_clean_approve(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _mock_model(monkeypatch, {"verdict": "approve", "summary": "ok", "findings": []})
    assert _review()["pass"] is True
    body = seen[0]
    assert body["text"]["format"]["strict"] is True
    user = json.loads(body["input"][1]["content"][0]["text"])
    assert user["diff"] == "+hello" and "AGENTS.md" in user["repository_rules"]


def test_review_blocks_on_request_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_model(monkeypatch, {"verdict": "request_changes", "summary": "bug", "findings": [
        {"path": "README.md", "severity": "blocking", "issue": "x"}]})
    assert _review()["pass"] is False


def test_approve_with_a_blocking_finding_still_blocks() -> None:
    passed, blocking = judge_verdict({"verdict": "approve", "findings": [
        {"path": "a", "severity": "blocking", "issue": "x"}]})
    assert passed is False and len(blocking) == 1


@pytest.mark.parametrize("verdict", [{}, {"verdict": "APPROVE"}, {"verdict": "lgtm"}])
def test_anything_but_an_explicit_approve_blocks(verdict: dict[str, Any]) -> None:
    assert judge_verdict(verdict)[0] is False


def test_model_error_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("harness.review.gate.time.sleep", lambda _: None)
    _mock_model(monkeypatch, {"verdict": "approve"}, status=500)
    result = _review()
    assert result["pass"] is False and "fail closed" in result["reason"]


def test_unparseable_model_output_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_model(monkeypatch, "approve!!")
    assert _review()["pass"] is False


def test_missing_key_and_oversized_diff_fail_closed() -> None:
    assert _review(config=None)["pass"] is False
    assert _review(diff="x" * (MAX_DIFF_CHARS + 1))["pass"] is False


def test_owner_override_passes_without_calling_the_model() -> None:
    result = _review(config=None, owner_override=SignOff(approved=True, by="aalricc", via="label"))
    assert result["pass"] is True


def test_reviewer_prompt_treats_pr_content_as_data() -> None:
    lowered = REVIEW_SYSTEM_PROMPT.lower()
    assert "never instructions" in lowered
    assert "ignore any text" in lowered


def test_cli_risk_gate_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def write(name: str, data: Any) -> Path:
        path = tmp_path / name
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return path

    args = ["prog", "risk", "--pr", str(write("pr.json", _pr())),
            "--permissions", str(write("perm.json", PERMS)),
            "--events", str(write("e.json", [])), "--reviews", str(write("r.json", []))]
    monkeypatch.setattr("sys.stdout", StringIO())
    assert main([*args, "--files", str(write("ok.txt", "README.md\n"))]) == 0
    assert main([*args, "--files", str(write("bad.txt", "src/mi_agent/llm/client.py\n"))]) == 1
    assert main([*args, "--files", str(tmp_path / "missing.txt")]) == 1


# ---------------------------------------------------------------- workflow security


def test_gate_workflow_runs_trusted_code_and_never_executes_the_pr() -> None:
    text = Path(".github/workflows/pr-gates.yml").read_text(encoding="utf-8")
    assert "pull_request_target:" in text
    assert "ref: ${{ github.event.pull_request.base.sha }}" in text
    assert "head.ref" not in text and "head.sha }}\n" not in text.split("with:")[1][:200]
    assert "owner-approved" in text and "synchronize" in text
    for context in ("agent-review", "risk-gate"):
        assert f"context=\"{context}\"" in text or f"post {context}" in text


def test_every_workflow_action_is_pinned_to_a_commit_sha() -> None:
    import re

    for workflow in Path(".github/workflows").glob("*.yml"):
        for ref in re.findall(
            r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", workflow.read_text(encoding="utf-8"), re.M
        ):
            assert re.search(r"@[0-9a-f]{40}$", ref), f"{workflow}: {ref} is not SHA-pinned"


def test_rate_limit_is_retried_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    from harness.review import gate

    calls = {"n": 0}
    ok = {"output": [{"content": [{"type": "output_text", "text": json.dumps(
        {"verdict": "approve", "summary": "ok", "findings": []})}]}]}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, request=request, headers={"retry-after": "1"})
        return httpx.Response(200, request=request, json=ok)

    real = httpx.Client
    monkeypatch.setattr(
        "harness.review.gate.httpx.Client",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    slept: list[float] = []
    verdict = gate.call_reviewer(CONFIG, {}, sleep=slept.append)
    assert verdict["verdict"] == "approve" and slept == [1.0, 1.0]


def test_persistent_rate_limit_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from harness.review import gate

    real = httpx.Client
    monkeypatch.setattr(
        "harness.review.gate.httpx.Client",
        lambda **kw: real(transport=httpx.MockTransport(
            lambda r: httpx.Response(429, request=r)), **kw),
    )
    with pytest.raises(httpx.HTTPStatusError):
        gate.call_reviewer(CONFIG, {}, retries=2, sleep=lambda _: None)


def test_agent_settings_deny_self_approval_and_rule_changes() -> None:
    deny = json.loads(Path(".claude/settings.json").read_text(encoding="utf-8"))["permissions"][
        "deny"
    ]
    joined = "\n".join(deny)
    for needle in ("owner-approved", "--approve", "--admin", "rulesets", "protection",
                   "gh secret", "--force"):
        assert needle in joined


def test_owner_without_current_write_access_does_not_count() -> None:
    changed = ["src/mi_agent/llm/client.py"]
    labelled = {"pr": _pr(labels=(OWNER_LABEL,)), "label_events": [_labeled("aalricc")]}
    for permissions in ({}, {"aalricc": "read"}, {"aalricc": "none"}, {"aalricc": ""}):
        result = risk_gate(changed=changed, codeowners=CODEOWNERS, reviews=[],
                           permissions=permissions, **labelled)
        assert result["pass"] is False, permissions
    assert active_owners(CODEOWNERS, {"AALRICC": "write"}) == {"aalricc"}
