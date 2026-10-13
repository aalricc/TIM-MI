from __future__ import annotations

from pathlib import Path

import pytest

from mi_agent.agent.selector import EpsilonGreedySelector, SelectableAction

pytestmark = pytest.mark.phase5


def _candidates() -> list[SelectableAction]:
    return [
        SelectableAction("a", "action_a", {}, 0.0),
        SelectableAction("b", "action_b", {}, 0.0),
    ]


def test_contextual_bandit_prefers_action_with_higher_observed_reward(test_settings) -> None:
    settings = test_settings.model_copy(update={"epsilon_start": 0.0, "epsilon_min": 0.0})
    selector = EpsilonGreedySelector(settings)

    selected, _, _ = selector.select(
        candidates=_candidates(),
        visits=1,
        has_proven_skill=False,
        failed_action_keys=set(),
        state_changed_since_failure=False,
    )
    selector.observe(action_key=selected.action_key, reward=1.0)

    for _ in range(6):
        selector.observe(action_key="a", reward=1.0)
        selector.observe(action_key="b", reward=-1.0)

    next_selected, _, mode = selector.select(
        candidates=_candidates(),
        visits=100,
        has_proven_skill=False,
        failed_action_keys=set(),
        state_changed_since_failure=False,
    )

    assert mode == "exploit"
    assert next_selected.action_key == "a"


def test_bandit_code_does_not_reference_harness_labels() -> None:
    selector_source = Path("src/mi_agent/agent/selector.py").read_text(encoding="utf-8")
    bandit_source = Path("src/mi_agent/agent/bandit.py").read_text(encoding="utf-8")

    for forbidden in ("scenario_id", "hidden_label", "reference_routes"):
        assert forbidden not in selector_source
        assert forbidden not in bandit_source


def test_eval_epsilon_overrides_decay_schedule(test_settings) -> None:
    settings = test_settings.model_copy(
        update={
            "epsilon_start": 0.8,
            "epsilon_min": 0.2,
            "epsilon_decay": 0.5,
            "eval_epsilon": 0.0,
        }
    )
    selector = EpsilonGreedySelector(settings)
    assert selector.epsilon(visits=0, has_proven_skill=False) == pytest.approx(0.0)
    assert selector.epsilon(visits=100, has_proven_skill=True) == pytest.approx(0.0)


def test_selector_breaks_ties_deterministically_by_action_key(test_settings) -> None:
    settings = test_settings.model_copy(update={"epsilon_start": 0.0, "epsilon_min": 0.0})
    selector = EpsilonGreedySelector(settings)
    candidates = [
        SelectableAction("a", "action_a", {}, 0.0),
        SelectableAction("b", "action_b", {}, 0.0),
    ]
    selected, _, mode = selector.select(
        candidates=candidates,
        visits=0,
        has_proven_skill=False,
        failed_action_keys=set(),
        state_changed_since_failure=False,
    )
    assert mode == "exploit"
    assert selected.action_key == "b"
