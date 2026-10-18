from __future__ import annotations

import os

import pytest

from mi_agent.config import MISettings
from mi_agent.llm.client import OpenAILLMClient
from mi_agent.workflow.models import AttemptFeedback

pytestmark = pytest.mark.live


@pytest.mark.asyncio
async def test_openai_preflight_and_structured_calls() -> None:
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("MI_OPENAI_API_KEY")
    if api_key is None or api_key.strip() == "":
        pytest.skip("OPENAI_API_KEY or MI_OPENAI_API_KEY is required for live integration test")

    settings = MISettings(
        openai_api_key=api_key,
        llm_endpoint=os.getenv("MI_LLM_ENDPOINT", "https://api.openai.com/v1/responses"),
        llm_model=os.getenv("MI_LLM_MODEL", "gpt-5.3-codex"),
        llm_timeout_seconds=30,
    )
    client = OpenAILLMClient(settings)

    preflight = await client.preflight()
    assert preflight["status"] == "ok"

    diagnosis = await client.diagnose_why(
        evidence={
            "health_deep": {"ok": False, "error": "db_corrupt"},
            "db_integrity": {"ok": False, "rows": ["malformed"]},
            "log_tail": ["sqlite3.DatabaseError: database disk image is malformed"],
        },
        rag_chunks=[],
    )
    assert diagnosis.failure_type != ""

    plan = await client.propose_how(
        diagnosis=diagnosis,
        rag_chunks=[],
        prior_attempts=[
            AttemptFeedback(
                tool_name="restore_1",
                operation="request_backup_restore",
                tool_error="backup_missing",
                verifier_details={"status": "still_unhealthy"},
            )
        ],
        latest_tool_error=None,
        state_fingerprint="test-fingerprint",
    )
    assert plan.primary.operation in {
        "request_backup_restore",
        "restart_service",
        "run_sql",
        "replace_file",
    }
    assert len(plan.alternatives) >= 2
