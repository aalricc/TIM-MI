from __future__ import annotations

import asyncio
import inspect
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from mi_agent.config import MISettings


@dataclass(slots=True)
class JevDecision:
    usable: bool
    reason: str | None = None
    selected_candidate_name: str | None = None
    selected_candidate_operation: str | None = None
    usable_probability: float | None = None
    risk_score: float | None = None
    model_version: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    shadow: dict[str, Any] = field(default_factory=dict)


class JevDecisionClient:
    def __init__(self, settings: MISettings) -> None:
        self._settings = settings

    @staticmethod
    def _candidate_label(candidate: dict[str, Any], limit: int = 300) -> str:
        params = json.dumps(candidate.get("params", {}), default=str, sort_keys=True)
        if len(params) > limit:
            params = params[:limit] + "..."
        return (f"{candidate.get('name', 'unknown')} "
                f"(operation={candidate.get('operation', 'unknown')}, params={params})")

    async def choose_candidate(
        self,
        *,
        state: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> JevDecision:
        if not candidates:
            return JevDecision(usable=False, reason="no_candidates")

        try:
            return await asyncio.to_thread(
                self._choose_candidate_sync,
                state,
                candidates,
            )
        except Exception as exc:  # pragma: no cover - depends on external Jev server behavior
            raw: dict[str, Any] = {
                "error": str(exc),
                "exception_type": type(exc).__name__,
                "state_summary": self._summarize_state_for_error(state),
                "candidate_summary": self._summarize_candidates_for_error(candidates),
            }

            module_name = type(exc).__module__
            if module_name and module_name != "builtins":
                raw["exception_module"] = module_name

            status_code = getattr(exc, "status", None)
            if not isinstance(status_code, int):
                status_code = getattr(exc, "status_code", None)
            if isinstance(status_code, int):
                raw["status"] = status_code

            endpoint = getattr(exc, "endpoint", None)
            if isinstance(endpoint, str) and endpoint.strip() != "":
                raw["endpoint"] = endpoint

            request_id = getattr(exc, "request_id", None)
            if isinstance(request_id, str) and request_id.strip() != "":
                raw["request_id"] = request_id

            body = self._sanitize_error_body(getattr(exc, "body", None))
            if body is not None:
                raw["body"] = body

            return JevDecision(
                usable=False,
                reason=f"jev_request_error:{type(exc).__name__}",
                raw=raw,
            )

    def _choose_candidate_sync(
        self,
        state: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> JevDecision:
        try:
            # Optional `jev` extra: missing in dev-only installs, typed when installed.
            from langchain_typesafe import (  # type: ignore[import-not-found, unused-ignore]
                Choice,
                Noul,
                Score,
                TypeSafeClassifier,
            )
        except ImportError as exc:  # pragma: no cover - exercised by environment, not unit tests
            return JevDecision(
                usable=False,
                reason="langchain_typesafe_not_installed",
                raw={"error": str(exc)},
            )

        option_to_candidate: dict[str, dict[str, Any]] = {}
        criteria: dict[str, Any] = {}
        for index, candidate in enumerate(candidates, start=1):
            option = f"option_{index}"
            option_to_candidate[option] = candidate
            criteria[option] = (
                f"tool={candidate.get('name', 'unknown')} "
                f"operation={candidate.get('operation', 'unknown')} "
                f"rationale={candidate.get('rationale', '')}"
            ).strip()

        questions: dict[str, Any] = {
            "can_execute_automatically": Noul(
                instructions=(
                    "At least one candidate is safe and appropriate for automatic execution now. "
                    "Return a higher probability when there is a concrete, reversible action "
                    "with executable parameters."
                )
            ),
            "risk_score": Score(
                instructions="Overall risk of automatic execution for selected action.",
                criteria=[
                    "Low blast radius and reversible.",
                    "Moderate risk or partial irreversibility.",
                    "High blast radius or destructive if wrong.",
                ],
            ),
        }
        if len(option_to_candidate) >= 2:
            questions["select_action"] = Choice(
                instructions=(
                    "Choose the single best recovery action for this incident state. "
                    "Prefer lowest-risk actions that can recover service quickly. "
                    "Prefer candidates with concrete executable parameters; treat "
                    "placeholder-style values as weaker options."
                ),
                criteria=criteria,
            )
        for option, candidate in option_to_candidate.items():
            label = self._candidate_label(candidate)
            questions[f"safe_{option}"] = Noul(
                instructions=(
                    "Is this action safe to run automatically right now and likely to fix "
                    f"the current failure: {label}? Answer no if it already failed in "
                    "prior_attempts."
                )
            )
            questions[f"destructive_{option}"] = Noul(
                instructions=(
                    "Would this action overwrite, delete, or irreversibly change data or "
                    f"configuration: {label}?"
                )
            )

        classifier = self._build_classifier(TypeSafeClassifier)
        payload = {
            "model": self._settings.jev_model,
            "state": state,
            "questions": questions,
        }
        response = self._invoke_classifier(classifier, payload)

        raw = self._to_dict(response)
        model_version = self._extract_str(response, raw, "model_version")
        choice_block: Any = None

        if len(option_to_candidate) >= 2:
            choice_block = self._question_block(
                response,
                raw,
                section="choices",
                question_id="select_action",
            )
            selected_option = self._extract_choice(choice_block)
            if selected_option is None or selected_option not in option_to_candidate:
                return JevDecision(
                    usable=False,
                    reason="invalid_choice_response",
                    model_version=model_version,
                    raw=raw,
                )
        else:
            selected_option = next(iter(option_to_candidate))

        try:
            def _p(qid: str) -> float | None:
                return self._extract_float(
                    self._question_block(response, raw, section="nouls", question_id=qid),
                    keys=("noul", "value", "probability"),
                )

            shadow: dict[str, Any] = {
                "selected_option": selected_option,
                "per_candidate": {},
            }
            for option, candidate in option_to_candidate.items():
                shadow["per_candidate"][option] = {
                    "operation": candidate.get("operation"),
                    "safe": _p(f"safe_{option}"),
                    "destructive": _p(f"destructive_{option}"),
                }
            if len(option_to_candidate) >= 2:
                probs = (
                    choice_block.get("probabilities")
                    if isinstance(choice_block, dict)
                    else getattr(choice_block, "probabilities", None)
                )
                if isinstance(probs, dict) and len(probs) >= 2:
                    top = sorted((float(value) for value in probs.values()), reverse=True)
                    shadow["choice_probabilities"] = probs
                    shadow["choice_margin"] = top[0] - top[1]
        except Exception:
            shadow = {"error": "shadow_extraction_failed"}

        usability_block = self._question_block(
            response,
            raw,
            section="nouls",
            question_id="can_execute_automatically",
        )
        usable_probability = self._extract_float(
            usability_block,
            keys=("noul", "value", "probability"),
        )
        if usable_probability is None:
            return JevDecision(
                usable=False,
                reason="missing_usable_probability",
                model_version=model_version,
                raw=raw,
            )

        threshold = self._settings.jev_usable_threshold
        if usable_probability < threshold:
            return JevDecision(
                usable=False,
                reason=f"usable_probability_below_threshold:{usable_probability:.3f}<{threshold:.3f}",
                selected_candidate_name=option_to_candidate[selected_option].get("name"),
                selected_candidate_operation=option_to_candidate[selected_option].get("operation"),
                usable_probability=usable_probability,
                model_version=model_version,
                raw=raw,
                shadow=shadow,
            )

        score_block = self._question_block(
            response,
            raw,
            section="scores",
            question_id="risk_score",
        )
        risk_score = self._extract_float(score_block, keys=("score", "value"))
        selected = option_to_candidate[selected_option]
        return JevDecision(
            usable=True,
            selected_candidate_name=str(selected.get("name", "")),
            selected_candidate_operation=str(selected.get("operation", "")),
            usable_probability=usable_probability,
            risk_score=risk_score,
            model_version=model_version,
            raw=raw,
            shadow=shadow,
        )

    def _build_classifier(self, classifier_cls: type[Any]) -> Any:
        kwargs: dict[str, Any] = {}
        signature = inspect.signature(classifier_cls)
        base_url = self._normalized_base_url(self._settings.jev_base_url)

        if "api_key" in signature.parameters and self._settings.jev_api_key is not None:
            kwargs["api_key"] = self._settings.jev_api_key
        if "model" in signature.parameters:
            kwargs["model"] = self._settings.jev_model
        if "base_url" in signature.parameters and base_url is not None:
            kwargs["base_url"] = base_url
        if "timeout" in signature.parameters:
            kwargs["timeout"] = self._settings.jev_timeout_seconds

        if self._settings.jev_api_key is not None:
            os.environ["TYPESAFE_API_KEY"] = self._settings.jev_api_key
        if base_url is not None:
            os.environ["TYPESAFE_BASE_URL"] = base_url
        return classifier_cls(**kwargs)

    @staticmethod
    def _normalized_base_url(base_url: str | None, *, in_docker: bool | None = None) -> str | None:
        if base_url is None:
            return None
        normalized = base_url.strip()
        if normalized == "":
            return None

        if in_docker is None:
            in_docker = Path("/.dockerenv").exists()
        if not in_docker:
            return normalized

        parsed = urlparse(normalized)
        hostname = parsed.hostname or ""
        if hostname not in {"127.0.0.1", "localhost"}:
            return normalized

        replacement_host = "host.docker.internal"
        netloc = replacement_host
        if parsed.port is not None:
            netloc = f"{replacement_host}:{parsed.port}"
        return urlunparse(parsed._replace(netloc=netloc))

    @staticmethod
    def _invoke_classifier(classifier: Any, payload: dict[str, Any]) -> Any:
        invoke = classifier.invoke
        try:
            return invoke(payload)
        except Exception:
            fallback_payload = dict(payload)
            fallback_payload.pop("model", None)
            return invoke(fallback_payload)

    @staticmethod
    def _to_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump()
            if isinstance(dumped, dict):
                return dumped
        return {"repr": repr(value)}

    @staticmethod
    def _extract_str(response: Any, raw: dict[str, Any], key: str) -> str | None:
        if isinstance(raw.get(key), str):
            return str(raw[key])
        attr = getattr(response, key, None)
        return attr if isinstance(attr, str) else None

    @staticmethod
    def _question_block(
        response: Any,
        raw: dict[str, Any],
        *,
        section: str,
        question_id: str,
    ) -> Any:
        raw_section = raw.get(section)
        if isinstance(raw_section, dict) and question_id in raw_section:
            return raw_section[question_id]
        if question_id in raw:
            return raw[question_id]

        attr_section = getattr(response, section, None)
        if isinstance(attr_section, dict) and question_id in attr_section:
            return attr_section[question_id]
        if attr_section is not None:
            get_method = getattr(attr_section, "get", None)
            if callable(get_method):
                item = get_method(question_id)
                if item is not None:
                    return item
        return getattr(response, question_id, None)

    @staticmethod
    def _extract_choice(block: Any) -> str | None:
        if isinstance(block, str):
            return block
        if isinstance(block, dict):
            for key in ("choice", "selected", "value"):
                value = block.get(key)
                if isinstance(value, str) and value != "":
                    return value
            probabilities = block.get("probabilities")
            if isinstance(probabilities, dict) and probabilities:
                best = max(probabilities.items(), key=lambda item: float(item[1]))
                if isinstance(best[0], str):
                    return best[0]

        for key in ("choice", "selected", "value"):
            value = getattr(block, key, None)
            if isinstance(value, str) and value != "":
                return value
        return None

    @staticmethod
    def _extract_float(block: Any, *, keys: tuple[str, ...]) -> float | None:
        if isinstance(block, (int, float)):
            return float(block)

        if isinstance(block, dict):
            for key in keys:
                value = block.get(key)
                if isinstance(value, (int, float)):
                    return float(value)

        for key in keys:
            value = getattr(block, key, None)
            if isinstance(value, (int, float)):
                return float(value)

        return None

    @classmethod
    def _summarize_state_for_error(cls, state: dict[str, Any]) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "keys": sorted(state.keys()),
            "types": {key: cls._value_type(value) for key, value in state.items()},
        }

        candidate_actions = state.get("candidate_actions")
        if isinstance(candidate_actions, list):
            summary["candidate_actions_count"] = len(candidate_actions)

        prior_attempts = state.get("prior_attempts")
        if isinstance(prior_attempts, list):
            summary["prior_attempts_count"] = len(prior_attempts)

        hypotheses = state.get("hypotheses")
        if isinstance(hypotheses, list):
            summary["hypotheses_count"] = len(hypotheses)

        context = state.get("class_memory_candidate")
        if isinstance(context, dict):
            summary["class_memory_candidate_keys"] = sorted(context.keys())
            summary["class_memory_candidate_types"] = {
                key: cls._value_type(value) for key, value in context.items()
            }

        return summary

    @classmethod
    def _summarize_candidates_for_error(
        cls,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        operations: list[str] = []
        shapes: list[dict[str, Any]] = []
        for candidate in candidates:
            operation = candidate.get("operation")
            if isinstance(operation, str):
                operations.append(operation)
            params = candidate.get("params")
            shape: dict[str, Any] = {
                "keys": sorted(candidate.keys()),
                "operation": operation if isinstance(operation, str) else None,
            }
            if isinstance(params, dict):
                shape["param_keys"] = sorted(params.keys())
                shape["param_types"] = {
                    key: cls._value_type(value) for key, value in params.items()
                }
            else:
                shape["params_type"] = cls._value_type(params)
            shapes.append(shape)
        return {
            "count": len(candidates),
            "operations": operations,
            "shapes": shapes,
        }

    @classmethod
    def _sanitize_error_body(cls, value: Any, *, depth: int = 0) -> Any:
        if value is None:
            return None

        if isinstance(value, (str, int, float, bool)):
            if isinstance(value, str) and len(value) > 500:
                return f"{value[:500]}..."
            return value

        if depth > 10:
            return "<truncated:depth>"

        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for key, item in value.items():
                if isinstance(key, str) and key.lower() in {"input", "request"}:
                    continue
                sanitized[str(key)] = cls._sanitize_error_body(item, depth=depth + 1)
            return sanitized

        if isinstance(value, list):
            return [cls._sanitize_error_body(item, depth=depth + 1) for item in value[:20]]

        if isinstance(value, tuple):
            return [cls._sanitize_error_body(item, depth=depth + 1) for item in value[:20]]

        return cls._truncate_text(repr(value), limit=500)

    @staticmethod
    def _value_type(value: Any) -> str:
        if value is None:
            return "NoneType"
        return type(value).__name__

    @staticmethod
    def _truncate_text(value: str, *, limit: int) -> str:
        if len(value) <= limit:
            return value
        return f"{value[:limit]}..."

