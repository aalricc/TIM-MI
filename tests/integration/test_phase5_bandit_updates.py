from __future__ import annotations

import pytest

from mi_agent.llm.client import FakeLLMClient
from mi_agent.runtime import build_agent

pytestmark = [pytest.mark.asyncio(), pytest.mark.phase5]


def _diagnosis() -> dict[str, object]:
    return {
        "failure_type": "db_file_missing",
        "hypotheses": ["database file missing"],
        "evidence": ["log signature matched"],
    }


def _apply_schema_step() -> dict[str, object]:
    return {
        "action_id": "apply_schema",
        "parameters": {"db_path": "data/app.db", "schema_path": "schema.sql"},
    }


async def _async_true() -> bool:
    return True


async def test_agent_feeds_selector_reward_updates_from_outcomes(test_settings) -> None:
    llm = FakeLLMClient()
    llm.queue_response("diagnose", _diagnosis())
    llm.queue_response(
        "propose_plans",
        [{"name": "recover", "rationale": "", "steps": [_apply_schema_step()]}],
    )
    llm.queue_response(
        "distill_skill",
        {
            "name": "recover-db-from-schema",
            "description": "Apply schema",
            "failure_type": "db_file_missing",
            "trigger_fingerprint": {},
            "steps": [_apply_schema_step()],
            "expected_postcondition": "healthy",
        },
    )

    agent, context = build_agent(test_settings, llm)
    context.verifier.health_endpoint_ok = lambda: _async_true()  # type: ignore[method-assign]

    observed: list[tuple[str, float]] = []
    real_observe = context.selector.observe

    def observe_spy(*, action_key: str, reward: float, features=None) -> None:  # type: ignore[no-untyped-def]
        observed.append((action_key, reward))
        real_observe(action_key=action_key, reward=reward, features=features)

    context.selector.observe = observe_spy  # type: ignore[method-assign]

    test_settings.db_file.unlink()
    test_settings.app_log.write_text(
        "sqlite3.OperationalError: unable to open database file\n",
        encoding="utf-8",
    )
    await agent._recover_loop()

    assert observed
    assert any(action_key.startswith("apply_schema|") for action_key, _ in observed)
    assert any(reward > 0.0 for _, reward in observed)

