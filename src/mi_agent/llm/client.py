from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from mi_agent.config import MISettings
from mi_agent.errors import LLMError
from mi_agent.workflow.models import AttemptFeedback, Diagnosis, HowProposal, RAGChunk


class LLMClient(ABC):
    @abstractmethod
    async def preflight(self) -> dict[str, str]:
        raise NotImplementedError

    @abstractmethod
    async def diagnose_why(
        self,
        *,
        evidence: Mapping[str, Any],
        rag_chunks: Sequence[RAGChunk],
    ) -> Diagnosis:
        raise NotImplementedError

    @abstractmethod
    async def propose_how(
        self,
        *,
        diagnosis: Diagnosis,
        rag_chunks: Sequence[RAGChunk],
        prior_attempts: Sequence[AttemptFeedback],
        latest_tool_error: str | None,
        state_fingerprint: str | None,
    ) -> HowProposal:
        raise NotImplementedError

    def consume_trace(self) -> dict[str, Any] | None:
        return None


class OpenAILLMClient(LLMClient):
    def __init__(self, settings: MISettings) -> None:
        self._settings = settings
        self._last_trace: dict[str, Any] | None = None

    async def preflight(self) -> dict[str, str]:
        payload = await self._call_json(
            instruction=(
                "Return JSON with exactly one field: ok='ok'."
            ),
            user_payload={"task": "preflight"},
            schema_name="llm_preflight",
            schema={
                "type": "object",
                "properties": {"ok": {"type": "string"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
        )
        if str(payload.get("ok", "")).lower() != "ok":
            raise LLMError("LLM preflight did not return expected payload")
        return {
            "model": self._settings.llm_model,
            "endpoint": self._settings.llm_endpoint,
            "status": "ok",
        }

    async def diagnose_why(
        self,
        *,
        evidence: Mapping[str, Any],
        rag_chunks: Sequence[RAGChunk],
    ) -> Diagnosis:
        payload = await self._call_json(
            instruction=(
                "Diagnose the root cause from diagnostics and RAG symptom context. "
                "Return JSON only. failure_type must be lower_snake_case."
            ),
            user_payload={
                "task": "diagnose_why",
                "evidence": dict(evidence),
                "rag_chunks": [chunk.model_dump() for chunk in rag_chunks],
            },
            schema_name="diagnosis",
            schema={
                "type": "object",
                "properties": {
                    "failure_type": {"type": "string"},
                    "hypotheses": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["failure_type", "hypotheses", "evidence"],
                "additionalProperties": False,
            },
        )
        return Diagnosis.model_validate(payload)

    async def propose_how(
        self,
        *,
        diagnosis: Diagnosis,
        rag_chunks: Sequence[RAGChunk],
        prior_attempts: Sequence[AttemptFeedback],
        latest_tool_error: str | None,
        state_fingerprint: str | None,
    ) -> HowProposal:
        payload = await self._call_json(
            instruction=(
                "Propose one primary dynamic recovery action and 2-4 ranked alternatives. "
                "Use only allowed operations. "
                "Do not output strategy text outside JSON."
            ),
            user_payload={
                "task": "propose_how",
                "diagnosis": diagnosis.model_dump(),
                "rag_chunks": [chunk.model_dump() for chunk in rag_chunks],
                "prior_attempts": [attempt.model_dump() for attempt in prior_attempts],
                "latest_tool_error": latest_tool_error,
                "state_fingerprint": state_fingerprint,
                "allowed_operations": [
                    "request_backup_restore",
                    "restart_service",
                    "run_sql",
                    "replace_file",
                ],
                "rules": {
                    "single_action": True,
                    "dynamic_tool_only": True,
                    "restart_service_is_fallback": True,
                    "alternatives_observability_only": True,
                },
            },
            schema_name="how_proposal",
            schema={
                "type": "object",
                "properties": {
                    "primary": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "rationale": {"type": "string"},
                            "operation": {
                                "type": "string",
                                "enum": [
                                    "request_backup_restore",
                                    "restart_service",
                                    "run_sql",
                                    "replace_file",
                                ],
                            },
                            "params": {"type": "object"},
                        },
                        "required": ["name", "rationale", "operation", "params"],
                        "additionalProperties": False,
                    },
                    "alternatives": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 4,
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "rationale": {"type": "string"},
                                "operation": {
                                    "type": "string",
                                    "enum": [
                                        "request_backup_restore",
                                        "restart_service",
                                        "run_sql",
                                        "replace_file",
                                    ],
                                },
                                "params": {"type": "object"},
                            },
                            "required": ["name", "rationale", "operation", "params"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["primary", "alternatives"],
                "additionalProperties": False,
            },
        )
        proposal = HowProposal.model_validate(payload)
        if proposal.primary.operation == "restart_service" and not prior_attempts:
            raise LLMError("restart_service proposed without prior failed attempts")
        return proposal

    def consume_trace(self) -> dict[str, Any] | None:
        trace = self._last_trace
        self._last_trace = None
        return trace

    async def _call_json(
        self,
        *,
        instruction: str,
        user_payload: Mapping[str, Any],
        schema_name: str,
        schema: Mapping[str, Any],
    ) -> dict[str, Any]:
        body = {
            "model": self._settings.llm_model,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": instruction}],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": json.dumps(user_payload)}
                    ],
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": dict(schema),
                    "strict": True,
                }
            },
        }

        response = await self._post(body)
        parsed = self._extract_json_text(response)
        if not isinstance(parsed, dict):
            raise LLMError("LLM response payload was not an object")
        self._last_trace = {
            "schema_name": schema_name,
            "instruction": instruction,
            "request": dict(user_payload),
            "response": response,
            "parsed": parsed,
        }
        return parsed

    async def _post(self, body: Mapping[str, Any]) -> dict[str, Any]:
        api_key = self._settings.openai_api_key
        headers = {"Content-Type": "application/json"}
        if self._settings.llm_auth_mode == "bearer":
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            headers[self._settings.llm_api_key_header] = api_key

        async with httpx.AsyncClient(timeout=self._settings.llm_timeout_seconds) as client:
            try:
                response = await client.post(
                    self._settings.llm_endpoint,
                    headers=headers,
                    json=body,
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise LLMError(f"LLM request failed: {exc}") from exc

        data = response.json()
        if not isinstance(data, dict):
            raise LLMError("LLM response was not a JSON object")
        return data

    @staticmethod
    def _extract_json_text(payload: dict[str, Any]) -> Any:
        output = payload.get("output")
        if not isinstance(output, list):
            raise LLMError("LLM response missing output list")

        for item in output:
            if not isinstance(item, dict):
                continue
            contents = item.get("content")
            if not isinstance(contents, list):
                continue
            for content in contents:
                if not isinstance(content, dict):
                    continue
                if content.get("type") in {"output_text", "text"}:
                    text = content.get("text")
                    if isinstance(text, str):
                        try:
                            return json.loads(text)
                        except json.JSONDecodeError as exc:
                            raise LLMError("LLM returned non-JSON text payload") from exc
        raise LLMError("LLM response did not include output text")
