from __future__ import annotations

import pytest

from mi_agent.agent.shield import ActionShield
from mi_agent.events.bus import EventBus
from mi_agent.tools.executor import ToolExecutor

pytestmark = pytest.mark.phase4


def test_pre_shield_uses_registry_risk_tier_for_candidate_gate(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = ToolExecutor(test_settings, EventBus())
    shield = ActionShield(test_settings, tools)
    calls = {"count": 0}

    original = tools.risk_tier

    def risk_tier_spy(primitive_id: str) -> str:
        calls["count"] += 1
        return original(primitive_id)

    monkeypatch.setattr(tools, "risk_tier", risk_tier_spy)

    outcome = shield.precheck_candidate(
        action_id="apply_schema",
        parameters={"db_path": "data/app.db", "schema_path": "schema.sql"},
        attempt_number=1,
    )

    assert outcome.allowed is True
    assert calls["count"] == 1
    assert any(key.startswith("db_path:") for key in outcome.parameter_state)


def test_pre_shield_blocks_when_incident_budget_exhausted(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())
    shield = ActionShield(test_settings, tools)

    outcome = shield.precheck_candidate(
        action_id="apply_schema",
        parameters={"db_path": "data/app.db", "schema_path": "schema.sql"},
        attempt_number=test_settings.max_attempts_per_incident + 1,
    )

    assert outcome.allowed is False
    assert outcome.reason == "incident_attempt_budget_exhausted"


def test_pre_shield_allows_when_incident_attempt_budget_disabled(test_settings) -> None:
    settings = test_settings.model_copy(update={"max_attempts_per_incident": 0})
    tools = ToolExecutor(settings, EventBus())
    shield = ActionShield(settings, tools)

    outcome = shield.precheck_candidate(
        action_id="apply_schema",
        parameters={"db_path": "data/app.db", "schema_path": "schema.sql"},
        attempt_number=999,
    )

    assert outcome.allowed is True


def test_pre_shield_blocks_irreversible_without_prior_read_observation(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())
    shield = ActionShield(test_settings, tools)

    outcome = shield.precheck_candidate(
        action_id="restart_service",
        parameters={"service_name": "target-app"},
        attempt_number=1,
        has_recent_read_observation=False,
    )

    assert outcome.allowed is False
    assert outcome.reason == "requires_prior_read_observation"


def test_post_shield_blocks_when_parameter_state_changes(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())
    shield = ActionShield(test_settings, tools)

    pre = shield.precheck_candidate(
        action_id="set_permissions",
        parameters={"path": "data/app.db", "mode": 0o660},
        attempt_number=1,
    )
    assert pre.allowed is True

    test_settings.db_file.unlink()

    post = shield.postcheck_candidate(
        action_id="set_permissions",
        parameters={"path": "data/app.db", "mode": 0o660},
        parameter_state=pre.parameter_state,
    )

    assert post.allowed is False
    assert post.reason == "state_changed_since_planning"
