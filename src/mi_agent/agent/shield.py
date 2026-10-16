from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mi_agent.config import MISettings
from mi_agent.errors import PrimitiveNotAllowedError, ToolError
from mi_agent.tools.executor import ToolExecutor
from mi_agent.tools.pathing import PathGuard

_KNOWN_RISK_TIERS = frozenset({"read_only", "reversible", "irreversible", "terminal", "brokered"})
_PATH_KEYS = (
    "path",
    "db_path",
    "source_db_path",
    "output_path",
    "source_path",
    "destination_path",
    "schema_path",
)


@dataclass(frozen=True, slots=True)
class ShieldDecision:
    allowed: bool
    reason: str | None = None
    parameter_state: dict[str, bool] = field(default_factory=dict)


class ActionShield:
    def __init__(self, settings: MISettings, tools: ToolExecutor) -> None:
        self._settings = settings
        self._tools = tools
        self._path_guard = PathGuard(settings)

    def precheck_candidate(
        self,
        *,
        action_id: str,
        parameters: dict[str, Any],
        attempt_number: int,
        has_recent_read_observation: bool = True,
    ) -> ShieldDecision:
        del attempt_number

        try:
            risk_tier = self._tools.risk_tier(action_id)
        except PrimitiveNotAllowedError:
            return ShieldDecision(allowed=False, reason="unknown_primitive")
        if risk_tier not in _KNOWN_RISK_TIERS:
            return ShieldDecision(allowed=False, reason="unknown_risk_tier")
        if risk_tier == "irreversible" and not has_recent_read_observation:
            return ShieldDecision(allowed=False, reason="requires_prior_read_observation")

        try:
            validated = self._tools.validate_action_schema(action_id, parameters)
        except ToolError:
            return ShieldDecision(allowed=False, reason="invalid_parameters")

        state = self._capture_parameter_state(action_id, validated.model_dump())
        return ShieldDecision(allowed=True, parameter_state=state)

    def postcheck_candidate(
        self,
        *,
        action_id: str,
        parameters: dict[str, Any],
        parameter_state: dict[str, bool],
    ) -> ShieldDecision:
        try:
            validated = self._tools.validate_action_schema(action_id, parameters)
        except ToolError:
            return ShieldDecision(allowed=False, reason="invalid_parameters")

        current_state = self._capture_parameter_state(action_id, validated.model_dump())
        if parameter_state != current_state:
            return ShieldDecision(allowed=False, reason="state_changed_since_planning")
        return ShieldDecision(allowed=True, parameter_state=current_state)

    def _capture_parameter_state(
        self,
        action_id: str,
        parameters: dict[str, Any],
    ) -> dict[str, bool]:
        state: dict[str, bool] = {}

        for key in _PATH_KEYS:
            value = parameters.get(key)
            if isinstance(value, str):
                resolved = self._resolve_path(action_id, key, value)
                state[f"{key}:{resolved.as_posix()}"] = resolved.exists() or resolved.is_symlink()

        paths_value = parameters.get("paths")
        if isinstance(paths_value, list):
            for index, path_value in enumerate(paths_value):
                if not isinstance(path_value, str):
                    continue
                resolved = self._resolve_path(action_id, "path", path_value)
                state[f"paths[{index}]:{resolved.as_posix()}"] = (
                    resolved.exists() or resolved.is_symlink()
                )

        return state

    def _resolve_path(self, action_id: str, key: str, value: str) -> Path:
        if key == "schema_path":
            return self._path_guard.resolve_app_file(value)
        if key == "path" and action_id in {"read_app_file", "write_app_file"}:
            return self._path_guard.resolve_app_file(value)
        return self._path_guard.resolve_in_sandbox(value)
