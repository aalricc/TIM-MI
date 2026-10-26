from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import httpx
import pytest

from harness.review.pr_review import main


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _capture_stdout(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> str:
    stream = StringIO()
    monkeypatch.setattr("sys.stdout", stream)
    assert main(argv) == 0
    return stream.getvalue()


def test_pr_review_skips_without_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _write(tmp_path / "changed.txt", "src/mi_agent/agent/engine.py\n")
    _write(tmp_path / "src/mi_agent/agent/engine.py", "def x():\n    return 1\n")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    rendered = _capture_stdout(monkeypatch, ["prog", "pr", "--files-file", str(files)])
    assert "Skipped: `OPENAI_API_KEY` is not set" in rendered


def test_reflect_skips_when_campaign_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = _write(
        tmp_path / "campaign.json",
        json.dumps({"overall_status": "pass"}),
    )

    rendered = _capture_stdout(
        monkeypatch,
        ["prog", "reflect", "--campaign-json", str(campaign)],
    )
    assert "Campaign passed; reflection skipped." in rendered


def test_pr_review_uses_model_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _write(tmp_path / "changed.txt", "src/mi_agent/llm/client.py\n")
    _write(tmp_path / "src/mi_agent/llm/client.py", "def why():\n    return 'ok'\n")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    payload = {
        "output": [{"content": [{"type": "output_text", "text": "## Summary\nLooks good."}]}]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://api.openai.com/v1/responses")
        return httpx.Response(200, request=request, json=payload)

    real_client = httpx.Client

    def client_factory(**kwargs: object) -> httpx.Client:
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("harness.review.pr_review.httpx.Client", client_factory)

    rendered = _capture_stdout(monkeypatch, ["prog", "pr", "--files-file", str(files)])
    assert "## Summary" in rendered


def test_selective_review_workflow_tracks_risky_paths() -> None:
    workflow = Path(".github/workflows/selective-llm-review.yml").read_text(encoding="utf-8")
    assert "pull_request:" in workflow
    assert "continue-on-error: true" in workflow
    assert "src/mi_agent/agent/engine.py" in workflow
    assert "src/mi_agent/decision/**" in workflow
    assert "src/mi_agent/llm/**" in workflow
    assert "src/mi_agent/workflow/models.py" in workflow
    assert "src/mi_agent/config.py" in workflow
    assert "src/mi_agent/settings.py" not in workflow
    assert "harness/contracts/*.schema.v*.json" in workflow
    assert "harness/sensors/**/*.py" in workflow
    assert "harness/metrics/thresholds.json" in workflow
    assert "\n      - harness/sensors/**\n" not in workflow
    assert "actions/github-script@v7" in workflow


def test_reflect_fallback_header_matches_reflection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = _write(
        tmp_path / "campaign.json",
        json.dumps({"overall_status": "fail", "scenarios": []}),
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    rendered = _capture_stdout(
        monkeypatch,
        ["prog", "reflect", "--campaign-json", str(campaign)],
    )

    assert "## Log-Quality Reflection" in rendered
