from __future__ import annotations

import pytest

from mi_agent.errors import LLMOutputValidationError
from mi_agent.llm.client import FakeLLMClient
from mi_agent.workflow.models import Diagnosis


@pytest.mark.asyncio()
async def test_fake_llm_malformed_output_rejected() -> None:
    llm = FakeLLMClient()
    llm.queue_response("propose_plans", {"unexpected": "shape"})

    with pytest.raises(LLMOutputValidationError):
        await llm.propose_plans(
            diagnosis=Diagnosis(
                failure_type="db_file_missing",
                hypotheses=["h"],
                evidence=["e"],
            ),
            available_actions=["ping_db"],
            action_parameter_contracts={"ping_db": {"required": [], "properties": {}}},
            recalled_skills=[],
        )


@pytest.mark.asyncio()
async def test_fake_llm_missing_response_raises() -> None:
    llm = FakeLLMClient()
    with pytest.raises(LLMOutputValidationError):
        await llm.write_lesson(failure_type="x", summary="y")
