from __future__ import annotations

import asyncio
import json
import os
import re
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, cast

import httpx
from pydantic import BaseModel

from mi_agent.config import MISettings
from mi_agent.errors import LLMOutputValidationError, LLMUnavailableError
from mi_agent.tools.schemas import CandidatePlan, LessonOutput, MatchSkillOutput, SkillProposal
from mi_agent.workflow.models import Diagnosis

_TModel = TypeVar("_TModel", bound=BaseModel)


class LLMClient(Protocol):
    async def diagnose(
        self,
        *,
        failure_type_hint: str | None,
        sanitized_logs: Sequence[str],
        probes: Mapping[str, Any],
        lessons: Sequence[str],
    ) -> Diagnosis: ...

    async def propose_plans(
        self,
        *,
        diagnosis: Diagnosis,
        available_actions: Sequence[str],
        action_parameter_contracts: Mapping[str, Any],
        recalled_skills: Sequence[str],
        failed_action_keys: Sequence[str] = (),
    ) -> list[CandidatePlan]: ...

    async def distill_skill(
        self,
        *,
        failure_type: str,
        episode_actions: Sequence[tuple[str, dict[str, Any], bool]],
        trigger_fingerprint: Mapping[str, str],
    ) -> SkillProposal: ...

    async def match_skill(
        self,
        *,
        failure_type: str,
        trigger_fingerprint: Mapping[str, str],
        candidate_skills: Sequence[str],
    ) -> MatchSkillOutput: ...

    async def write_lesson(
        self,
        *,
        failure_type: str,
        summary: str,
    ) -> str: ...


@dataclass(slots=True)
class LLMUsage:
    calls: int = 0
    failures: int = 0
    estimated_cost_usd: float = 0.0


class OpenAILLMClient:
    def __init__(self, settings: MISettings) -> None:
        self._settings = settings
        self.usage = LLMUsage()

    async def diagnose(
        self,
        *,
        failure_type_hint: str | None,
        sanitized_logs: Sequence[str],
        probes: Mapping[str, Any],
        lessons: Sequence[str],
    ) -> Diagnosis:
        payload = {
            "task": "diagnose",
            "failure_type_hint": failure_type_hint,
            "logs": list(sanitized_logs),
            "probes": dict(probes),
            "lessons": list(lessons),
        }
        return await self._call_structured(
            schema=Diagnosis,
            task="diagnose",
            payload=payload,
            instruction=(
                "Diagnose the incident using logs/probes/lessons. "
                "Return structured JSON only."
            ),
        )

    async def propose_plans(
        self,
        *,
        diagnosis: Diagnosis,
        available_actions: Sequence[str],
        action_parameter_contracts: Mapping[str, Any],
        recalled_skills: Sequence[str],
        failed_action_keys: Sequence[str] = (),
    ) -> list[CandidatePlan]:
        payload = {
            "task": "propose_plans",
            "diagnosis": diagnosis.model_dump(),
            "available_actions": list(available_actions),
            "action_parameter_contracts": dict(action_parameter_contracts),
            "recalled_skills": list(recalled_skills),
            "failed_action_keys": list(failed_action_keys),
            "rules": {
                "max_steps": 5,
                "allowed_actions_only": True,
                "return_ranked_candidates": True,
                "all_required_step_parameters_must_be_provided": True,
                "avoid_failed_actions_when_state_unchanged": True,
            },
        }
        plans_wrapper = await self._call_structured(
            schema=_PlanCandidates,
            task="propose_plans",
            payload=payload,
            instruction=(
                "Propose ranked candidate plans for recovery. "
                "Each plan must use only available_actions and typed parameters. "
                "For each step, include all required parameters from action_parameter_contracts. "
                "Avoid repeating failed_action_keys unless there is clear new evidence. "
                "Return structured JSON only."
            ),
        )
        return plans_wrapper.plans

    async def distill_skill(
        self,
        *,
        failure_type: str,
        episode_actions: Sequence[tuple[str, dict[str, Any], bool]],
        trigger_fingerprint: Mapping[str, str],
    ) -> SkillProposal:
        payload = {
            "task": "distill_skill",
            "failure_type": failure_type,
            "episode_actions": [
                {"action_id": a, "parameters": p, "success": s} for a, p, s in episode_actions
            ],
            "trigger_fingerprint": dict(trigger_fingerprint),
        }
        return await self._call_structured(
            schema=SkillProposal,
            task="distill_skill",
            payload=payload,
            instruction=(
                "Distill a reusable skill from the successful episode. "
                "Use only observed successful steps. Return structured JSON only."
            ),
        )

    async def match_skill(
        self,
        *,
        failure_type: str,
        trigger_fingerprint: Mapping[str, str],
        candidate_skills: Sequence[str],
    ) -> MatchSkillOutput:
        payload = {
            "task": "match_skill",
            "failure_type": failure_type,
            "trigger_fingerprint": dict(trigger_fingerprint),
            "candidate_skills": list(candidate_skills),
        }
        return await self._call_structured(
            schema=MatchSkillOutput,
            task="match_skill",
            payload=payload,
            instruction=(
                "Choose the best skill match and confidence in [0,1]. "
                "Return structured JSON only."
            ),
        )

    async def write_lesson(
        self,
        *,
        failure_type: str,
        summary: str,
    ) -> str:
        payload = {"task": "write_lesson", "failure_type": failure_type, "summary": summary}
        lesson = await self._call_structured(
            schema=LessonOutput,
            task="write_lesson",
            payload=payload,
            instruction=(
                "Write a concise sanitized lesson (<=500 chars) for future incidents. "
                "Return structured JSON only."
            ),
        )
        return lesson.lesson

    async def _call_structured(
        self,
        *,
        schema: type[_TModel],
        task: str,
        payload: dict[str, Any],
        instruction: str,
    ) -> _TModel:
        response = await self._call(
            task=task,
            payload=payload,
            schema_name=task,
            json_schema=self._provider_json_schema(schema.model_json_schema()),
            instruction=instruction,
        )
        try:
            return schema.model_validate(response)
        except Exception as exc:
            raise LLMOutputValidationError(str(exc)) from exc

    async def _call(
        self,
        *,
        task: str,
        payload: dict[str, Any],
        schema_name: str,
        json_schema: dict[str, Any],
        instruction: str,
    ) -> Any:
        self.usage.calls += 1
        timeout = httpx.Timeout(self._settings.llm_timeout_seconds)
        retries = self._settings.llm_retries
        api_key = self._resolve_api_key()
        if api_key is None or api_key.strip() == "":
            self.usage.failures += 1
            raise LLMUnavailableError("OPENAI_API_KEY (or MI_OPENAI_API_KEY) is not configured")
        for attempt in range(retries + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    headers = {"Content-Type": "application/json"}
                    if self._settings.llm_auth_mode == "bearer":
                        headers["Authorization"] = f"Bearer {api_key}"
                    else:
                        headers[self._settings.llm_api_key_header] = api_key

                    response = await client.post(
                        self._settings.llm_endpoint,
                        headers=headers,
                        json={
                            "model": self._settings.llm_model,
                            "input": [
                                {
                                    "role": "system",
                                    "content": [
                                        {
                                            "type": "input_text",
                                            "text": (
                                                "You are MI agent reasoning engine. "
                                                "Never emit shell/code. "
                                                "Output valid JSON matching the schema only."
                                            ),
                                        }
                                    ],
                                },
                                {
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "input_text",
                                            "text": (
                                                f"Task: {task}\n"
                                                f"Instruction: {instruction}\n"
                                                "Input JSON:\n"
                                                f"{json.dumps(payload, ensure_ascii=False)}"
                                            ),
                                        }
                                    ],
                                },
                            ],
                            "text": {
                                "format": {
                                    "type": "json_schema",
                                    "name": schema_name,
                                    "schema": json_schema,
                                    "strict": True,
                                }
                            },
                        },
                    )
                if response.status_code >= 500:
                    raise LLMUnavailableError(f"LLM server error: {response.status_code}")
                if self._is_rate_limited(response):
                    raise LLMUnavailableError(f"LLM rate limited: {response.text}")
                if response.status_code >= 400:
                    raise LLMOutputValidationError(response.text)
                body = response.json()
                usage = body.get("usage", {})
                total_tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
                if isinstance(total_tokens, int):
                    self.usage.estimated_cost_usd += total_tokens * 0.000001
                else:
                    self.usage.estimated_cost_usd += 0.001
                return self._extract_json_payload(body)
            except (httpx.TimeoutException, httpx.ConnectError, LLMUnavailableError) as exc:
                if attempt >= retries:
                    self.usage.failures += 1
                    raise LLMUnavailableError(str(exc)) from exc
                await asyncio.sleep(0.2 * (attempt + 1))

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        if response.status_code == 429:
            return True
        if response.status_code < 400:
            return False
        try:
            body = response.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                code = error.get("code")
                err_type = error.get("type")
                if code == "rate_limit_exceeded" or err_type == "too_many_requests":
                    return True
        text = response.text.lower()
        return "rate_limit_exceeded" in text or "too_many_requests" in text

    def _resolve_api_key(self) -> str | None:
        configured = self._settings.openai_api_key
        if configured is not None and configured.strip() != "":
            return configured
        from_env = os.getenv("OPENAI_API_KEY") or os.getenv("MI_OPENAI_API_KEY")
        if from_env is not None and from_env.strip() != "":
            return from_env
        return None

    @staticmethod
    def _extract_json_payload(body: dict[str, Any]) -> Any:
        if isinstance(body.get("output"), dict):
            return body["output"]
        output_text = OpenAILLMClient._extract_output_text(body)
        cleaned = output_text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise LLMOutputValidationError(
                f"Model did not return valid JSON: {cleaned[:200]}"
            ) from exc

    @staticmethod
    def _extract_output_text(body: dict[str, Any]) -> str:
        direct = body.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct

        output = body.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for chunk in content:
                    if not isinstance(chunk, dict):
                        continue
                    chunk_text = chunk.get("text")
                    if isinstance(chunk_text, str) and chunk_text.strip():
                        return chunk_text
        raise LLMOutputValidationError("Unable to extract text output from LLM response")

    @classmethod
    def _provider_json_schema(cls, raw_schema: dict[str, Any]) -> dict[str, Any]:
        schema = cast(dict[str, Any], json.loads(json.dumps(raw_schema)))
        cls._enforce_closed_objects(schema)
        return schema

    @classmethod
    def _enforce_closed_objects(cls, node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                cls._enforce_closed_objects(item)
            return

        if not isinstance(node, dict):
            return

        node_type = node.get("type")
        if node_type == "object":
            properties = node.get("properties")
            if not isinstance(properties, dict):
                properties = {}
                node["properties"] = properties
            node["additionalProperties"] = False
            node["required"] = sorted(properties.keys())

        for key in ("properties", "$defs", "definitions"):
            value = node.get(key)
            if isinstance(value, dict):
                for child in value.values():
                    cls._enforce_closed_objects(child)

        for key in ("items", "if", "then", "else", "not"):
            child = node.get(key)
            if child is not None:
                cls._enforce_closed_objects(child)

        for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            child_list = node.get(key)
            if isinstance(child_list, list):
                for child in child_list:
                    cls._enforce_closed_objects(child)


class _PlanCandidates(BaseModel):
    plans: list[CandidatePlan]


class FakeLLMClient:
    def __init__(self) -> None:
        self._responses: dict[str, deque[Any]] = defaultdict(deque)
        self.calls: dict[str, int] = defaultdict(int)
        self.failures: dict[str, int] = defaultdict(int)

    def queue_response(self, method: str, payload: Any) -> None:
        self._responses[method].append(payload)

    def queue_failure(self, method: str, times: int = 1) -> None:
        for _ in range(times):
            self._responses[method].append(LLMUnavailableError("fake outage"))

    async def diagnose(
        self,
        *,
        failure_type_hint: str | None,
        sanitized_logs: Sequence[str],
        probes: Mapping[str, Any],
        lessons: Sequence[str],
    ) -> Diagnosis:
        del failure_type_hint, sanitized_logs, probes, lessons
        return cast(Diagnosis, self._take("diagnose", Diagnosis))

    async def propose_plans(
        self,
        *,
        diagnosis: Diagnosis,
        available_actions: Sequence[str],
        action_parameter_contracts: Mapping[str, Any],
        recalled_skills: Sequence[str],
        failed_action_keys: Sequence[str] = (),
    ) -> list[CandidatePlan]:
        del diagnosis, available_actions, action_parameter_contracts, recalled_skills
        del failed_action_keys
        raw = self._take("propose_plans", list)
        return [CandidatePlan.model_validate(item) for item in raw]

    async def distill_skill(
        self,
        *,
        failure_type: str,
        episode_actions: Sequence[tuple[str, dict[str, Any], bool]],
        trigger_fingerprint: Mapping[str, str],
    ) -> SkillProposal:
        del failure_type, episode_actions, trigger_fingerprint
        return cast(SkillProposal, self._take("distill_skill", SkillProposal))

    async def match_skill(
        self,
        *,
        failure_type: str,
        trigger_fingerprint: Mapping[str, str],
        candidate_skills: Sequence[str],
    ) -> MatchSkillOutput:
        del failure_type, trigger_fingerprint, candidate_skills
        return cast(MatchSkillOutput, self._take("match_skill", MatchSkillOutput))

    async def write_lesson(
        self,
        *,
        failure_type: str,
        summary: str,
    ) -> str:
        del failure_type, summary
        output = self._take("write_lesson", (str, LessonOutput))
        if isinstance(output, LessonOutput):
            return output.lesson
        return str(output)

    def _take(self, method: str, expected: type[Any] | tuple[type[Any], ...]) -> Any:
        self.calls[method] += 1
        if not self._responses[method]:
            raise LLMOutputValidationError(f"No queued response for method {method}")
        next_item = self._responses[method].popleft()
        if isinstance(next_item, Exception):
            self.failures[method] += 1
            raise next_item
        if isinstance(next_item, BaseModel):
            return next_item
        if expected is list:
            if not isinstance(next_item, list):
                raise LLMOutputValidationError(f"Expected list for {method}")
            return next_item
        valid_types = expected if isinstance(expected, tuple) else (expected,)
        for valid_type in valid_types:
            if isinstance(next_item, valid_type):
                return next_item
            if isinstance(valid_type, type) and issubclass(valid_type, BaseModel):
                return valid_type.model_validate(next_item)
        raise LLMOutputValidationError(f"Unexpected response for {method}: {type(next_item)}")
