from __future__ import annotations

import pytest

from mi_agent.config import MISettings
from mi_agent.errors import LLMError
from mi_agent.llm.client import OpenAILLMClient
from mi_agent.workflow.models import AttemptFeedback, Diagnosis, RAGChunk


class _FallbackClient(OpenAILLMClient):
    def __init__(self, settings: MISettings) -> None:
        super().__init__(settings)
        self.calls: list[dict[str, object]] = []

    async def _call_json(
        self,
        *,
        instruction: str,
        user_payload: dict[str, object],
        schema_name: str,
        schema: dict[str, object],
        strict: bool = True,
    ) -> dict[str, object]:
        del instruction, schema
        self.calls.append(
            {
                "schema_name": schema_name,
                "strict": strict,
                "payload": user_payload,
            }
        )
        if len(self.calls) == 1:
            raise LLMError("LLM request failed: Client error '400 Bad Request' for url 'x'")
        return {
            "primary": {
                "name": "restore",
                "rationale": "best",
                "operation": "request_backup_restore",
                "params": {"backup_id": "latest"},
            },
            "alternatives": [
                {
                    "name": "repair",
                    "rationale": "alt1",
                    "operation": "run_sql",
                    "params": {"db_path": "data/app.db", "sql": "PRAGMA integrity_check;"},
                },
                {
                    "name": "restart",
                    "rationale": "alt2",
                    "operation": "restart_service",
                    "params": {"service_name": "target-app"},
                },
            ],
        }


@pytest.mark.asyncio
async def test_propose_how_retries_compact_payload_after_400(settings: MISettings) -> None:
    client = _FallbackClient(settings)

    diagnosis = Diagnosis(
        failure_type="schema_invalid",
        hypotheses=["widgets table missing"],
        evidence=["no such table: widgets"],
    )
    rag_chunks = [
        RAGChunk(source=f"doc-{index}", text="x" * 2000)
        for index in range(5)
    ]
    prior_attempts = [
        AttemptFeedback(
            tool_name=f"tool-{index}",
            operation="run_sql",
            tool_error="boom",
            verifier_details={"detail": "y" * 600},
        )
        for index in range(6)
    ]

    plan = await client.propose_how(
        diagnosis=diagnosis,
        rag_chunks=rag_chunks,
        prior_attempts=prior_attempts,
        latest_tool_error="z" * 1000,
        state_fingerprint="state",
    )

    assert plan.primary.operation == "request_backup_restore"
    assert len(client.calls) == 2

    first = client.calls[0]
    second = client.calls[1]

    assert first["schema_name"] == "how_proposal"
    assert first["strict"] is True
    assert second["schema_name"] == "how_proposal_compact"
    assert second["strict"] is False

    second_payload = second["payload"]
    assert isinstance(second_payload, dict)
    second_rag = second_payload["rag_chunks"]
    assert isinstance(second_rag, list)
    assert len(second_rag) == 3
    assert all(len(str(item["text"])) <= 1200 for item in second_rag)

