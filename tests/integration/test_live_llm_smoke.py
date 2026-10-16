from __future__ import annotations

import pytest

from mi_agent.config import MISettings
from mi_agent.llm.client import OpenAILLMClient


@pytest.mark.live
def test_live_llm_smoke() -> None:
    settings = MISettings()
    client = OpenAILLMClient(settings)

    async def _run() -> str:
        await client.preflight()
        return await client.write_lesson(
            failure_type="db_file_missing",
            summary="Recovered by applying schema after DB deletion",
        )

    import asyncio

    result = asyncio.run(_run())
    assert isinstance(result, str)
    assert result
