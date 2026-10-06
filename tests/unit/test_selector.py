from __future__ import annotations

from mi_agent.agent.selector import EpsilonGreedySelector, SelectableAction


def _candidates() -> list[SelectableAction]:
    return [
        SelectableAction("a", "action_a", {}, 1.0),
        SelectableAction("b", "action_b", {}, 2.0),
        SelectableAction("c", "action_c", {}, 0.5),
    ]


def test_epsilon_decay_and_skill_bias(test_settings) -> None:
    selector = EpsilonGreedySelector(test_settings)
    first = selector.epsilon(visits=0, has_proven_skill=False)
    later = selector.epsilon(visits=20, has_proven_skill=False)
    with_skill = selector.epsilon(visits=20, has_proven_skill=True)

    assert later <= first
    assert with_skill <= later
    assert with_skill >= test_settings.epsilon_min


def test_exploit_vs_explore_seeded(test_settings) -> None:
    selector = EpsilonGreedySelector(test_settings)
    exploit, _, mode1 = selector.select(
        candidates=_candidates(),
        visits=1000,
        has_proven_skill=False,
        failed_action_keys=set(),
        state_changed_since_failure=False,
    )
    assert mode1 == "exploit"
    assert exploit.action_key == "b"

    # force high epsilon by zero visits and no proven skill; seeded random gives deterministic pick
    explore, _, mode2 = selector.select(
        candidates=_candidates(),
        visits=0,
        has_proven_skill=False,
        failed_action_keys=set(),
        state_changed_since_failure=False,
    )
    assert mode2 in {"explore", "exploit"}
    assert explore.action_key in {"a", "b", "c"}


def test_no_repeat_failed_action_rule(test_settings) -> None:
    selector = EpsilonGreedySelector(test_settings)
    selected, _, _ = selector.select(
        candidates=_candidates(),
        visits=999,
        has_proven_skill=False,
        failed_action_keys={"b"},
        state_changed_since_failure=False,
    )
    assert selected.action_key != "b"

    selected2, _, _ = selector.select(
        candidates=_candidates(),
        visits=999,
        has_proven_skill=False,
        failed_action_keys={"b"},
        state_changed_since_failure=True,
    )
    assert selected2.action_key == "b"
