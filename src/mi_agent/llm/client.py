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
        backup_catalog: Sequence[Mapping[str, Any]],
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
        backup_catalog: Sequence[Mapping[str, Any]],
    ) -> HowProposal:
        instruction = (
            "Propose one primary dynamic recovery action and 2-4 ranked alternatives. "
            "Any candidate may be selected by Jev. "
            "Use only allowed operations. "
            "Do not output strategy text outside JSON."
        )
        schema = self._how_schema()
        try:
            payload = await self._call_json(
                instruction=instruction,
                user_payload=self._how_user_payload(
                    diagnosis=diagnosis,
                    rag_chunks=rag_chunks,
                    prior_attempts=prior_attempts,
                    latest_tool_error=latest_tool_error,
                    state_fingerprint=state_fingerprint,
                    backup_catalog=backup_catalog,
                    compact=False,
                ),
                schema_name="how_proposal",
                schema=schema,
                strict=True,
            )
        except LLMError as exc:
            if "400 Bad Request" not in str(exc):
                raise
            payload = await self._call_json(
                instruction=instruction,
                user_payload=self._how_user_payload(
                    diagnosis=diagnosis,
                    rag_chunks=rag_chunks,
                    prior_attempts=prior_attempts,
                    latest_tool_error=latest_tool_error,
                    state_fingerprint=state_fingerprint,
                    backup_catalog=backup_catalog,
                    compact=True,
                ),
                schema_name="how_proposal_compact",
                schema=schema,
                strict=False,
            )
        proposal = HowProposal.model_validate(payload)
        if proposal.primary.operation == "restart_service" and not prior_attempts:
            raise LLMError("restart_service proposed without prior failed attempts")
        return proposal

    @staticmethod
    def _how_schema() -> dict[str, Any]:
        return {
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
        }

    def _how_user_payload(
        self,
        *,
        diagnosis: Diagnosis,
        rag_chunks: Sequence[RAGChunk],
        prior_attempts: Sequence[AttemptFeedback],
        latest_tool_error: str | None,
        state_fingerprint: str | None,
        backup_catalog: Sequence[Mapping[str, Any]],
        compact: bool,
    ) -> dict[str, Any]:
        rag_rows = [chunk.model_dump() for chunk in rag_chunks]
        prior_rows = [attempt.model_dump() for attempt in prior_attempts]
        latest_error = latest_tool_error
        backup_rows = [dict(row) for row in backup_catalog]

        if compact:
            rag_rows = [
                {
                    **chunk,
                    "text": str(chunk.get("text", ""))[:1200],
                }
                for chunk in rag_rows[:3]
            ]
            prior_rows = [
                {
                    **attempt,
                    "verifier_details": {},
                }
                for attempt in prior_rows[-4:]
            ]
            if latest_error is not None:
                latest_error = latest_error[:400]
            backup_rows = backup_rows[:5]

        return {
            "task": "propose_how",
            "diagnosis": diagnosis.model_dump(),
            "rag_chunks": rag_rows,
            "prior_attempts": prior_rows,
            "latest_tool_error": latest_error,
            "state_fingerprint": state_fingerprint,
            "backup_catalog": backup_rows,
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
                "candidate_list_for_jev_selection": True,
            },
        }

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
        strict: bool = True,
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
                    "strict": strict,
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
            # One retry for transient failures (timeouts, connection errors, 429/5xx).
            for attempt in range(2):
                try:
                    response = await client.post(
                        self._settings.llm_endpoint,
                        headers=headers,
                        json=body,
                    )
                    response.raise_for_status()
                    break
                except httpx.HTTPError as exc:
                    if attempt == 0 and self._is_transient(exc):
                        continue
                    # str() of a timeout is often empty, so always name the type.
                    raise LLMError(
                        f"LLM request failed: {type(exc).__name__}: {exc}"
                    ) from exc

        data = response.json()
        if not isinstance(data, dict):
            raise LLMError("LLM response was not a JSON object")
        return data

    @staticmethod
    def _is_transient(exc: httpx.HTTPError) -> bool:
        if isinstance(exc, httpx.TransportError):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            return status == 429 or status >= 500
        return False

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
