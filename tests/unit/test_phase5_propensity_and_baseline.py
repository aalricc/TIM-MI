from __future__ import annotations

import pytest

from mi_agent.agent.selector import EpsilonGreedySelector
from mi_agent.llm.client import FakeLLMClient
from mi_agent.runtime import build_agent

pytestmark = pytest.mark.phase5


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


def test_selector_has_no_seeded_priors(test_settings) -> None:
    selector = EpsilonGreedySelector(test_settings)

    assert selector._bandit._state == {}


def test_v1_tabular_policy_baseline_retained(test_settings) -> None:
    _, context = build_agent(test_settings, FakeLLMClient())
    assert context.policy_version == "v1-tabular"


@pytest.mark.asyncio()
async def test_decision_log_records_propensities_for_each_decision(test_settings) -> None:
    llm = FakeLLMClient()
    llm.queue_response("diagnose", _diagnosis())
    llm.queue_response(
        "propose_plans",
        [
            {
                "name": "restart-first",
                "rationale": "quick bounce",
                "steps": [{"action_id": "restart_service", "parameters": {}}],
            }
        ],
    )
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

    test_settings.db_file.unlink()
    test_settings.app_log.write_text(
        "sqlite3.OperationalError: unable to open database file\n",
        encoding="utf-8",
    )
    await agent._recover_loop()

    entries = context.decision_log.read_all()
    assert len(entries) >= 2
    for entry in entries:
        assert entry.candidate_set
        assert entry.chosen_action in entry.candidate_set
        assert set(entry.propensities).issubset(set(entry.candidate_set))
        assert pytest.approx(sum(entry.propensities.values()), abs=1e-9) == 1.0
