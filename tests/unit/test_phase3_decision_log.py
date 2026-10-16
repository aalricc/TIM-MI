from __future__ import annotations

from pathlib import Path

import pytest

from mi_agent.agent.selector import EpsilonGreedySelector, SelectableAction
from mi_agent.llm.client import FakeLLMClient
from mi_agent.policy.decision_log import DecisionLogEntry, DecisionLogStore, RewardComponents
from mi_agent.runtime import build_agent

pytestmark = pytest.mark.phase3


def _candidates() -> list[SelectableAction]:
    return [
        SelectableAction("a", "action_a", {}, 1.0),
        SelectableAction("b", "action_b", {}, 2.0),
        SelectableAction("c", "action_c", {}, 0.5),
    ]


def _apply_schema_step() -> dict[str, object]:
    return {
        "action_id": "apply_schema",
        "parameters": {"db_path": "data/app.db", "schema_path": "schema.sql"},
    }


def test_selector_propensity_distribution_sums_to_one(test_settings) -> None:
    selector = EpsilonGreedySelector(test_settings)
    distribution = selector.propensity_distribution(
        candidates=_candidates(),
        visits=3,
        has_proven_skill=False,
        failed_action_keys=set(),
        state_changed_since_failure=False,
    )

    assert set(distribution) == {"a", "b", "c"}
    assert pytest.approx(sum(distribution.values()), abs=1e-9) == 1.0
    assert all(value >= 0.0 for value in distribution.values())


def test_decision_log_store_round_trip(tmp_path: Path) -> None:
    store = DecisionLogStore(tmp_path / "decision_log.jsonl")
    store.append(
        DecisionLogEntry(
            incident_id="inc-1",
            failure_type="db_file_missing",
            state_features={"probe_fingerprint": "abc"},
            candidate_set=["a", "b"],
            chosen_action="b",
            propensities={"a": 0.2, "b": 0.8},
            policy_version="v1-tabular",
            verifier_healthy=True,
            reward_components=RewardComponents(net_reward=1.2, verifier_success=1.0),
        )
    )

    entries = store.read_all()
    assert len(entries) == 1
    assert entries[0].schema_version == "v1"
    assert entries[0].chosen_action == "b"


@pytest.mark.asyncio()
async def test_agent_records_versioned_decision_log_entry(test_settings) -> None:
    llm = FakeLLMClient()
    llm.queue_response(
        "diagnose",
        {
            "failure_type": "db_file_missing",
            "hypotheses": ["db file missing"],
            "evidence": ["log signature"],
        },
    )
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
    assert entries
    entry = entries[-1]
    assert entry.schema_version == "v1"
    assert entry.policy_version == test_settings.policy_version
    assert isinstance(entry.verifier_healthy, bool)
    assert entry.chosen_action in entry.candidate_set
    assert pytest.approx(sum(entry.propensities.values()), abs=1e-9) == 1.0
    assert isinstance(entry.reward_components.net_reward, float)


async def _async_true() -> bool:
    return True
