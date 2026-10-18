from __future__ import annotations

import asyncio
import inspect
import os
from dataclasses import dataclass, field
from typing import Any

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


class JevDecisionClient:
    def __init__(self, settings: MISettings) -> None:
        self._settings = settings

    async def choose_candidate(
        self,
        *,
        state: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> JevDecision:
        if not candidates:
            return JevDecision(usable=False, reason="no_candidates")

        return await asyncio.to_thread(
            self._choose_candidate_sync,
            state,
            candidates,
        )

    def _choose_candidate_sync(
        self,
        state: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> JevDecision:
        try:
            from langchain_typesafe import (  # type: ignore[import-not-found]
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
        criteria: dict[str, str] = {}
        for index, candidate in enumerate(candidates, start=1):
            option = f"option_{index}"
            option_to_candidate[option] = candidate
            criteria[option] = (
                f"tool={candidate.get('name', 'unknown')} "
                f"operation={candidate.get('operation', 'unknown')} "
                f"rationale={candidate.get('rationale', '')}"
            ).strip()

        questions: dict[str, Any] = {
            "select_action": Choice(
                instructions=(
                    "Choose the single best recovery action for this incident state. "
                    "Prefer lowest-risk actions that can recover service quickly."
                ),
                criteria=criteria,
            ),
            "can_execute_automatically": Noul(
                instructions=(
                    "At least one candidate is safe and appropriate for automatic execution now."
                )
            ),
            "risk_score": Score(
                instructions="Overall risk of automatic execution for selected action.",
                criteria={
                    "low": "Low blast radius and reversible.",
                    "medium": "Moderate risk or partial irreversibility.",
                    "high": "High blast radius or destructive if wrong.",
                },
            ),
        }

        classifier = self._build_classifier(TypeSafeClassifier)
        payload = {
            "model": self._settings.jev_model,
            "state": state,
            "questions": questions,
        }
        response = self._invoke_classifier(classifier, payload)

        raw = self._to_dict(response)
        model_version = self._extract_str(response, raw, "model_version")

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
        )

    def _build_classifier(self, classifier_cls: type[Any]) -> Any:
        kwargs: dict[str, Any] = {}
        signature = inspect.signature(classifier_cls)

        if "api_key" in signature.parameters and self._settings.jev_api_key is not None:
            kwargs["api_key"] = self._settings.jev_api_key
        if "model" in signature.parameters:
            kwargs["model"] = self._settings.jev_model
        if "base_url" in signature.parameters and self._settings.jev_base_url is not None:
            kwargs["base_url"] = self._settings.jev_base_url
        if "timeout" in signature.parameters:
            kwargs["timeout"] = self._settings.jev_timeout_seconds

        original_api_key = os.getenv("TYPESAFE_API_KEY")
        if self._settings.jev_api_key is not None:
            os.environ["TYPESAFE_API_KEY"] = self._settings.jev_api_key
        try:
            return classifier_cls(**kwargs)
        finally:
            if self._settings.jev_api_key is not None:
                if original_api_key is None:
                    os.environ.pop("TYPESAFE_API_KEY", None)
                else:
                    os.environ["TYPESAFE_API_KEY"] = original_api_key

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

