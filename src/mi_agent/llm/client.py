from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from mi_agent.config import MISettings
from mi_agent.errors import LLMError
from mi_agent.llm.tracing import USAGE_FIELDS, llm_span_outputs, traced
from mi_agent.workflow.models import AttemptFeedback, Diagnosis, HowProposal, RAGChunk

# System prompts for the two reasoning stages. They describe the agent's role, the
# evidence it receives and the action interface; they deliberately name no fault, no
# expected fix and no preferred operation, so that which action works is learned from
# verified outcomes (policy store, class memory) rather than prompted.
WHY_INSTRUCTION = (
    "You are the diagnosis (WHY) stage of an autonomous recovery agent that keeps a "
    "SQLite-backed web service healthy. The service's deep health check just failed.\n"
    "Input JSON fields: evidence.health_deep (the failing health response), "
    "evidence.db_status (the service's own database report: existence, size, integrity, "
    "tables, columns, row count), evidence.db_integrity (PRAGMA integrity_check run by the "
    "agent), evidence.runtime_paths (which files exist), evidence.app_schema (the DDL the "
    "application expects), evidence.log_tail (recent application log lines, oldest first), "
    "evidence.backups (the backup catalog), and rag_chunks (SQLite documentation and "
    "learned skills retrieved for these symptoms).\n"
    "Task: identify the root cause of the outage from this evidence only.\n"
    "- failure_type: one short lower_snake_case label for the underlying condition (what "
    "is wrong with the system), not the symptom (not 'health_check_failed').\n"
    "- hypotheses: 1-4 candidate root causes, most likely first.\n"
    "- evidence: the specific fields and values from the input that support the first "
    "hypothesis, quoted or paraphrased exactly; never invent values.\n"
    "If the evidence cannot distinguish a cause, use failure_type 'unknown_failure' and "
    "say in hypotheses what evidence is missing. Return JSON only."
)

HOW_INSTRUCTION = (
    "You are the repair-planning (HOW) stage of an autonomous recovery agent that keeps a "
    "SQLite-backed web service healthy. Propose concrete repair actions for the diagnosed "
    "failure. A separate decision model (Jev) picks exactly one of your candidates; it is "
    "executed once, then the service's deep health check verifies it. The goal is a "
    "service whose health check passes and whose normal reads and writes succeed.\n"
    "Input JSON fields: diagnosis (from the WHY stage), incident_state (health, database "
    "report, integrity, file existence and the application's expected schema, collected "
    "when the incident was detected), "
    "backup_catalog (backups that exist now, newest first), prior_attempts (actions already "
    "tried for this incident; verifier_details is the state after each one), "
    "latest_tool_error, and "
    "rag_chunks (SQLite documentation and skills learned from earlier verified "
    "recoveries).\n"
    "Operations and their params (use exactly these keys):\n"
    "- request_backup_restore: {\"backup_id\": an id copied verbatim from "
    "backup_catalog}. The service replaces its database file with that backup.\n"
    "- run_sql: {\"sql\": SQLite script run with executescript, \"db_path\": path "
    "relative to incident_state.runtime_paths.app_root}. Fails if the database file is "
    "missing.\n"
    "- replace_file: {\"source_path\": path relative to "
    "incident_state.runtime_paths.sandbox_root, \"destination_path\": path relative to "
    "incident_state.runtime_paths.app_root}. Copies an existing file over another; the "
    "two roots differ, so derive each path from the absolute paths in runtime_paths.\n"
    "- restart_service: {\"service_name\": \"target-app\"}. Only allowed after at "
    "least one failed attempt (prior_attempts non-empty).\n"
    "Rules: every candidate must be a complete, executable action with valid params. "
    "Never repeat an action from prior_attempts with the same params; use its tool_error "
    "and verifier_details to choose something that addresses why it failed. Any SQL must "
    "leave the schema compatible with incident_state.app_schema. Give a primary and 2-4 "
    "distinct alternatives, each with a one-sentence rationale grounded in the input. "
    "Return JSON only."
)

# Diagnostics fields the HOW stage sees as incident_state (the log tail stays with WHY).
_HOW_STATE_FIELDS = ("health_deep", "db_status", "db_integrity", "runtime_paths", "app_schema")


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
        current_state: Mapping[str, Any] | None = None,
    ) -> HowProposal:
        raise NotImplementedError

    def consume_trace(self) -> dict[str, Any] | None:
        return None

    def consume_usage(self) -> dict[str, int] | None:
        """Token usage of the last diagnose_why / propose_how call, summed over its
        requests; None when the provider reported none."""
        return None


class OpenAILLMClient(LLMClient):
    def __init__(self, settings: MISettings) -> None:
        self._settings = settings
        self._last_trace: dict[str, Any] | None = None
        self._usage: dict[str, int] | None = None

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

    @traced("llm.why")
    async def diagnose_why(
        self,
        *,
        evidence: Mapping[str, Any],
        rag_chunks: Sequence[RAGChunk],
    ) -> Diagnosis:
        self._usage = None
        payload = await self._call_json(
            instruction=WHY_INSTRUCTION,
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

    @traced("llm.how")
    async def propose_how(
        self,
        *,
        diagnosis: Diagnosis,
        rag_chunks: Sequence[RAGChunk],
        prior_attempts: Sequence[AttemptFeedback],
        latest_tool_error: str | None,
        state_fingerprint: str | None,
        backup_catalog: Sequence[Mapping[str, Any]],
        current_state: Mapping[str, Any] | None = None,
    ) -> HowProposal:
        instruction = HOW_INSTRUCTION
        schema = self._how_schema()
        self._usage = None
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
                    current_state=current_state,
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
                    current_state=current_state,
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
        current_state: Mapping[str, Any] | None,
        compact: bool,
    ) -> dict[str, Any]:
        rag_rows = [chunk.model_dump() for chunk in rag_chunks]
        prior_rows = [attempt.model_dump() for attempt in prior_attempts]
        latest_error = latest_tool_error
        backup_rows = [dict(row) for row in backup_catalog]
        state = {
            name: current_state[name]
            for name in _HOW_STATE_FIELDS
            if current_state is not None and name in current_state
        }

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
            "incident_state": state,
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

    def consume_usage(self) -> dict[str, int] | None:
        usage = self._usage
        self._usage = None
        return usage

    def _add_usage(self, response: Mapping[str, Any]) -> None:
        # Responses API: {"usage": {"input_tokens", "output_tokens", "total_tokens", ...}}.
        usage = response.get("usage")
        if not isinstance(usage, Mapping):
            return
        counts = {
            name: value
            for name in USAGE_FIELDS
            if isinstance(value := usage.get(name), int) and not isinstance(value, bool)
        }
        if not counts:
            return
        total = self._usage or {}
        self._usage = {name: total.get(name, 0) + counts.get(name, 0) for name in USAGE_FIELDS}

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
        self._add_usage(response)
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

    @traced("openai.responses", run_type="llm", process_outputs=llm_span_outputs)
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
