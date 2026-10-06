from __future__ import annotations

from pathlib import Path

import pytest

from mi_agent.errors import CorruptStoreError
from mi_agent.policy.store import PolicyStore
from mi_agent.workflow.models import EpisodeAction


def test_value_updates_and_credit_assignment(tmp_path: Path) -> None:
    store = PolicyStore(tmp_path / "policy.json")
    actions = [
        EpisodeAction(action_key="a", reward=1.0, duration_seconds=1.0, success=True),
        EpisodeAction(action_key="a", reward=-1.0, duration_seconds=1.0, success=False),
        EpisodeAction(action_key="b", reward=2.0, duration_seconds=1.0, success=True),
    ]
    store.update_episode("db_file_missing", actions)

    a = store.value_for("db_file_missing", "a")
    b = store.value_for("db_file_missing", "b")

    assert a.visits == 2
    assert a.avg_reward == pytest.approx(0.0)
    assert a.success_count == 1
    assert a.failure_count == 1

    assert b.visits == 1
    assert b.avg_reward == pytest.approx(2.0)


def test_lessons_recall_bounded(tmp_path: Path) -> None:
    store = PolicyStore(tmp_path / "policy.json")
    for i in range(25):
        store.record_lesson("db_file_missing", f"lesson-{i}")
    lessons = store.recall_lessons("db_file_missing")
    assert len(lessons) == 20
    assert lessons[0] == "lesson-5"


def test_corrupt_store_raises(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(CorruptStoreError):
        PolicyStore(path)


def test_failure_type_keys_are_canonicalized(tmp_path: Path) -> None:
    store = PolicyStore(tmp_path / "policy.json")
    store.update_episode(
        "database_initialization_storage_missing",
        [EpisodeAction(action_key="apply_schema|", reward=1.0, duration_seconds=0.2, success=True)],
    )

    value = store.value_for("db_file_missing", "apply_schema|")
    assert value.visits == 1
    assert "db_file_missing" in store.snapshot().failures
