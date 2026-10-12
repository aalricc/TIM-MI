from __future__ import annotations

import pytest

from mi_agent.agent.executor import AgentActionExecutor
from mi_agent.agent.reward import score_outcome
from mi_agent.events.bus import EventBus
from mi_agent.policy.store import PolicyStore
from mi_agent.tools.executor import ToolExecutor
from mi_agent.workflow.models import EpisodeAction

pytestmark = pytest.mark.phase5


def test_reward_model_applies_tier_and_data_loss_costs() -> None:
    read_only_success = score_outcome(
        verifier_healthy=True,
        duration_seconds=6.0,
        attempts=1,
        risk_tier="read_only",
        rolled_back=False,
        used_last_resort=False,
        data_loss_ratio=0.0,
    )
    irreversible_success = score_outcome(
        verifier_healthy=True,
        duration_seconds=6.0,
        attempts=1,
        risk_tier="irreversible",
        rolled_back=False,
        used_last_resort=False,
        data_loss_ratio=0.0,
    )
    failed_with_loss = score_outcome(
        verifier_healthy=False,
        duration_seconds=6.0,
        attempts=2,
        risk_tier="reversible",
        rolled_back=True,
        used_last_resort=False,
        data_loss_ratio=0.3,
    )

    assert irreversible_success.net_reward < read_only_success.net_reward
    assert failed_with_loss.net_reward < 0.0
    assert failed_with_loss.rollback_penalty > 0.0


def test_policy_store_supports_recency_weighting(tmp_path) -> None:
    path = tmp_path / "policy.json"

    ema_store = PolicyStore(path, recency_alpha=0.5)
    ema_store.update_episode(
        "db_file_missing",
        [
            EpisodeAction(action_key="a", reward=1.0, duration_seconds=1.0, success=True),
            EpisodeAction(action_key="a", reward=1.0, duration_seconds=1.0, success=True),
            EpisodeAction(action_key="a", reward=-1.0, duration_seconds=1.0, success=False),
        ],
    )

    recency_value = ema_store.value_for("db_file_missing", "a")
    assert recency_value.visits == 3
    assert recency_value.avg_reward == pytest.approx(0.0)


@pytest.mark.asyncio()
async def test_failed_action_memory_expires_by_ttl(test_settings) -> None:
    clock = {"now": 10.0}
    executor = AgentActionExecutor(
        ToolExecutor(test_settings, EventBus()),
        EventBus(),
        failed_action_ttl_seconds=5.0,
        now_fn=lambda: clock["now"],
    )

    action_key = executor.action_key("ping_db", {})
    executor.mark_failed(action_key, "state-1")

    clock["now"] = 16.0
    result = await executor.execute_primitive(
        action_id="ping_db",
        parameters={},
        allowed_primitives=frozenset({"ping_db"}),
        state_fingerprint="state-1",
    )
    assert result.success


@pytest.mark.asyncio()
async def test_failed_action_memory_invalidates_on_state_change(test_settings) -> None:
    clock = {"now": 20.0}
    executor = AgentActionExecutor(
        ToolExecutor(test_settings, EventBus()),
        EventBus(),
        failed_action_ttl_seconds=100.0,
        now_fn=lambda: clock["now"],
    )

    action_key = executor.action_key("ping_db", {})
    executor.mark_failed(action_key, "state-1")

    first = await executor.execute_primitive(
        action_id="ping_db",
        parameters={},
        allowed_primitives=frozenset({"ping_db"}),
        state_fingerprint="state-2",
    )
    second = await executor.execute_primitive(
        action_id="ping_db",
        parameters={},
        allowed_primitives=frozenset({"ping_db"}),
        state_fingerprint="state-1",
    )

    assert first.success
    assert second.success

