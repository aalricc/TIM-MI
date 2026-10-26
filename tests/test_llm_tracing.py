from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from mi_agent.config import MISettings
from mi_agent.llm.client import OpenAILLMClient
from mi_agent.llm.tracing import llm_span_outputs, span_inputs

langsmith = pytest.importorskip("langsmith")

API_KEY = "sk-test-secret-key"


def _response() -> dict[str, Any]:
    text = json.dumps({"failure_type": "db_missing", "hypotheses": ["h"], "evidence": ["e"]})
    return {
        "output": [{"content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7, "extra": {"x": 1}},
    }


def _serve(monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]) -> None:
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body, request=request)

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("mi_agent.llm.client.httpx.AsyncClient", client_factory)


def _runs(client: MagicMock, method: str) -> list[dict[str, Any]]:
    return [call.kwargs for call in client.method_calls if call[0] == method]


def test_span_inputs_drop_the_client_instance() -> None:
    assert span_inputs({"self": object(), "evidence": {"a": 1}}) == {"evidence": {"a": 1}}


def test_llm_span_outputs_expose_usage_metadata() -> None:
    assert llm_span_outputs(_response())["usage_metadata"] == {
        "input_tokens": 3,
        "output_tokens": 4,
        "total_tokens": 7,
    }
    assert llm_span_outputs({"output": []}) == {"output": []}
    assert llm_span_outputs("raw") == {"output": "raw"}


@pytest.mark.asyncio
async def test_diagnose_why_is_traced_as_a_chain_with_an_llm_child(
    settings: MISettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _response())
    ls_client = MagicMock(spec=langsmith.Client)
    llm = OpenAILLMClient(settings.model_copy(update={"openai_api_key": API_KEY}))

    with langsmith.tracing_context(enabled=True, client=ls_client):
        diagnosis = await llm.diagnose_why(evidence={"probe": 1}, rag_chunks=[])

    assert diagnosis.failure_type == "db_missing"
    created = _runs(ls_client, "create_run")
    assert [(run["name"], run["run_type"]) for run in created] == [
        ("llm.why", "chain"),
        ("openai.responses", "llm"),
    ]
    assert created[0]["inputs"] == {"evidence": {"probe": 1}, "rag_chunks": []}
    assert created[1]["parent_run_id"] == created[0]["id"]
    finished = {run["name"]: run for run in _runs(ls_client, "update_run")}
    assert finished["openai.responses"]["outputs"]["usage_metadata"]["total_tokens"] == 7
    assert finished["llm.why"]["outputs"]["failure_type"] == "db_missing"
    assert API_KEY not in repr(ls_client.method_calls)


@pytest.mark.asyncio
async def test_nothing_is_sent_when_tracing_is_disabled(
    settings: MISettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _response())
    ls_client = MagicMock(spec=langsmith.Client)
    llm = OpenAILLMClient(settings.model_copy(update={"openai_api_key": API_KEY}))

    with langsmith.tracing_context(enabled=False, client=ls_client):
        await llm.diagnose_why(evidence={}, rag_chunks=[])

    assert ls_client.method_calls == []
