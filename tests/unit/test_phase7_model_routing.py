from __future__ import annotations

from typing import Any

import httpx
import pytest

from mi_agent.errors import LLMPermanentError
from mi_agent.llm.client import OpenAILLMClient
from mi_agent.workflow.models import Diagnosis

pytestmark = pytest.mark.phase7


def test_live_llm_requires_mandatory_model(test_settings) -> None:
    with pytest.raises(LLMPermanentError):
        OpenAILLMClient(test_settings.model_copy(update={"llm_model": "gpt-4o-mini"}))


@pytest.mark.asyncio()
async def test_propose_plans_uses_mandatory_model_only(test_settings) -> None:
    client = OpenAILLMClient(test_settings)

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
    assert called_models == ["gpt-5.3-codex"]


def test_permanent_http_status_mapping(test_settings) -> None:
    client = OpenAILLMClient(test_settings)
    unauthorized = httpx.Response(401, request=httpx.Request("POST", "https://example.invalid"))
    forbidden = httpx.Response(403, request=httpx.Request("POST", "https://example.invalid"))
    not_found = httpx.Response(404, request=httpx.Request("POST", "https://example.invalid"))
    too_many = httpx.Response(429, request=httpx.Request("POST", "https://example.invalid"))

    assert client._permanent_error_for_status(unauthorized) is not None
    assert client._permanent_error_for_status(forbidden) is not None
    assert client._permanent_error_for_status(not_found) is not None
    assert client._permanent_error_for_status(too_many) is None
