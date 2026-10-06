from __future__ import annotations

import os

import pytest

from mi_agent.config import MISettings
from mi_agent.llm.client import OpenAILLMClient


@pytest.mark.asyncio()
@pytest.mark.skipif(
    os.getenv("MI_RUN_LIVE_LLM_TEST") != "1",
    reason="Set MI_RUN_LIVE_LLM_TEST=1 to run live LLM smoke test",
)
async def test_live_llm_smoke() -> None:
    settings = MISettings()
    client = OpenAILLMClient(settings)
    result = await client.write_lesson(
        failure_type="db_file_missing",
        summary="Recovered by applying schema after DB deletion",
    )
    assert isinstance(result, str)
    assert result
