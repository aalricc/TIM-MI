from __future__ import annotations

import os
from pathlib import Path

import pytest

from harness.contracts import contract_errors
from harness.review import judge as judge_mod
from harness.review.sample import sample_packets
from tests.harness_scenarios import SCENARIOS


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_judge_single_packet(tmp_path: Path) -> None:
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key is None or api_key == "":
        pytest.skip("OPENAI_API_KEY is not set")

    run = await SCENARIOS["full_loop_success"](tmp_path)
    packet = sample_packets(run.events, n=1)[0]

    verdict = judge_mod._judge_packet(
        packet,
        model="gpt-5.3",
        endpoint="https://api.openai.com/v1/responses",
        api_key=api_key,
        rubric_text=judge_mod._read_rubric(),
        approved_actions=judge_mod._extract_approved_actions_sections(),
    )

    assert contract_errors(verdict, "judge_verdict") == []
