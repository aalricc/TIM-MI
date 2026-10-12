from __future__ import annotations

from typing import Any

import httpx
import pytest

from mi_agent.errors import LLMOutputValidationError
from mi_agent.llm.client import OpenAILLMClient
from mi_agent.workflow.models import Diagnosis

pytestmark = pytest.mark.phase7


@pytest.mark.asyncio()
async def test_propose_plans_uses_small_model_then_escalates_on_validation_failure(
    test_settings,
) -> None:
    settings = test_settings.model_copy(
        update={
            "llm_model": "default-model",
            "llm_small_model": "small-model",
            "llm_large_model": "large-model",
            "llm_escalate_on_validation_failure": True,
        }
    )
    client = OpenAILLMClient(settings)

    called_models: list[str] = []

    async def _fake_call(
        *,
        task: str,
        payload: dict[str, Any],
        schema_name: str,
        json_schema: dict[str, Any],
        instruction: str,
        model: str,
    ) -> Any:
        del task, payload, schema_name, json_schema, instruction
        called_models.append(model)
        if len(called_models) == 1:
            return {"unexpected": "shape"}
        return {
            "plans": [
                {
                    "name": "recover",
                    "rationale": "wait and retry",
                    "steps": [
                        {
                            "action_id": "wait",
                            "parameters": {"seconds": 1.0},
                            "preconditions": ["probe:db_ping=false"],
                            "expected_postconditions": ["probe:db_ping=true"],
                        }
                    ],
                }
            ]
        }

    client._call = _fake_call  # type: ignore[method-assign]

    plans = await client.propose_plans(
        diagnosis=Diagnosis(
            failure_type="db_file_missing",
            hypotheses=["db missing"],
            evidence=["log"],
        ),
        available_actions=["wait"],
        action_parameter_contracts={},
        recalled_skills=[],
        failed_action_keys=[],
    )

    assert len(plans) == 1
    assert called_models == ["small-model", "large-model"]


@pytest.mark.asyncio()
async def test_validation_failure_does_not_escalate_when_disabled(test_settings) -> None:
    settings = test_settings.model_copy(
        update={
            "llm_model": "default-model",
            "llm_small_model": "small-model",
            "llm_large_model": "large-model",
            "llm_escalate_on_validation_failure": False,
        }
    )
    client = OpenAILLMClient(settings)

    called_models: list[str] = []

    async def _fake_call(
        *,
        task: str,
        payload: dict[str, Any],
        schema_name: str,
        json_schema: dict[str, Any],
        instruction: str,
        model: str,
    ) -> Any:
        del task, payload, schema_name, json_schema, instruction
        called_models.append(model)
        return {"unexpected": "shape"}

    client._call = _fake_call  # type: ignore[method-assign]

    with pytest.raises(LLMOutputValidationError):
        await client.propose_plans(
            diagnosis=Diagnosis(
                failure_type="db_file_missing",
                hypotheses=["db missing"],
                evidence=["log"],
            ),
            available_actions=["wait"],
            action_parameter_contracts={},
            recalled_skills=[],
            failed_action_keys=[],
        )

    assert called_models == ["small-model"]


@pytest.mark.asyncio()
async def test_non_planning_tasks_keep_default_model(test_settings) -> None:
    settings = test_settings.model_copy(
        update={
            "llm_model": "default-model",
            "llm_small_model": "small-model",
            "llm_large_model": "large-model",
            "llm_escalate_on_validation_failure": True,
        }
    )
    client = OpenAILLMClient(settings)

    called_models: list[str] = []

    async def _fake_call(
        *,
        task: str,
        payload: dict[str, Any],
        schema_name: str,
        json_schema: dict[str, Any],
        instruction: str,
        model: str,
    ) -> Any:
        del task, payload, schema_name, json_schema, instruction
        called_models.append(model)
        return {
            "failure_type": "db_file_missing",
            "hypotheses": ["db missing"],
            "evidence": ["log"],
        }

    client._call = _fake_call  # type: ignore[method-assign]

    diagnosis = await client.diagnose(
        failure_type_hint=None,
        sanitized_logs=["log line"],
        probes={"db_ping": False},
        lessons=[],
    )

    assert diagnosis.failure_type == "db_file_missing"
    assert called_models == ["default-model"]


@pytest.mark.asyncio()
async def test_propose_plans_uses_large_fallback_when_small_model_unset(test_settings) -> None:
    settings = test_settings.model_copy(
        update={
            "llm_model": "default-model",
            "llm_small_model": None,
            "llm_large_model": "large-model",
            "llm_escalate_on_validation_failure": True,
        }
    )
    client = OpenAILLMClient(settings)

    called_models: list[str] = []

    async def _fake_call(
        *,
        task: str,
        payload: dict[str, Any],
        schema_name: str,
        json_schema: dict[str, Any],
        instruction: str,
        model: str,
    ) -> Any:
        del task, payload, schema_name, json_schema, instruction
        called_models.append(model)
        if len(called_models) == 1:
            return {"unexpected": "shape"}
        return {
            "plans": [
                {
                    "name": "recover",
                    "rationale": "wait and retry",
                    "steps": [
                        {
                            "action_id": "wait",
                            "parameters": {"seconds": 1.0},
                            "preconditions": ["probe:db_ping=false"],
                            "expected_postconditions": ["probe:db_ping=true"],
                        }
                    ],
                }
            ]
        }

    client._call = _fake_call  # type: ignore[method-assign]

    plans = await client.propose_plans(
        diagnosis=Diagnosis(
            failure_type="db_file_missing",
            hypotheses=["db missing"],
            evidence=["log"],
        ),
        available_actions=["wait"],
        action_parameter_contracts={},
        recalled_skills=[],
        failed_action_keys=[],
    )

    assert len(plans) == 1
    assert called_models == ["default-model", "large-model"]


@pytest.mark.asyncio()
async def test_propose_plans_instruction_includes_signature_self_check(test_settings) -> None:
    settings = test_settings.model_copy(
        update={
            "llm_model": "default-model",
            "llm_small_model": "small-model",
            "llm_large_model": "large-model",
            "llm_escalate_on_validation_failure": True,
        }
    )
    client = OpenAILLMClient(settings)

    seen_instruction = ""
    seen_payload: dict[str, Any] = {}

    async def _fake_call(
        *,
        task: str,
        payload: dict[str, Any],
        schema_name: str,
        json_schema: dict[str, Any],
        instruction: str,
        model: str,
    ) -> Any:
        nonlocal seen_instruction, seen_payload
        del task, schema_name, json_schema, model
        seen_instruction = instruction
        seen_payload = payload
        return {
            "plans": [
                {
                    "name": "recover",
                    "rationale": "wait and retry",
                    "steps": [
                        {
                            "action_id": "wait",
                            "parameters": {"seconds": 1.0},
                            "preconditions": ["probe:db_ping=false"],
                            "expected_postconditions": ["probe:db_ping=true"],
                        }
                    ],
                }
            ]
        }

    client._call = _fake_call  # type: ignore[method-assign]

    plans = await client.propose_plans(
        diagnosis=Diagnosis(
            failure_type="db_file_missing",
            hypotheses=["db missing"],
            evidence=["log"],
        ),
        available_actions=["wait"],
        action_parameter_contracts={},
        recalled_skills=[],
        failed_action_keys=[],
    )

    assert len(plans) == 1
    assert "Never repeat a step signature" in seen_instruction
    assert "self-check every plan for duplicate step signatures" in seen_instruction
    rules = seen_payload["rules"]
    assert rules["no_repeated_step_signatures"] is True
    assert "no_duplicate_step_signatures" in rules["self_check_before_emit"]


@pytest.mark.asyncio()
async def test_call_retries_on_remote_protocol_disconnect(test_settings, monkeypatch) -> None:
    settings = test_settings.model_copy(
        update={
            "openai_api_key": "test-key",
            "llm_retries": 1,
            "llm_timeout_seconds": 1.0,
        }
    )
    client = OpenAILLMClient(settings)

    attempts = {"count": 0}

    class _FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {"output": {"ok": True}, "usage": {"total_tokens": 1}}

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            return None

        async def post(self, *args, **kwargs):
            del args, kwargs
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise httpx.RemoteProtocolError("server disconnected")
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    payload = await client._call(
        task="diagnose",
        payload={"x": 1},
        schema_name="diagnose",
        json_schema={"type": "object"},
        instruction="instruction",
        model="test-model",
    )

    assert payload == {"ok": True}
    assert attempts["count"] == 2

