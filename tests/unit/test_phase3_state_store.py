from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

import pytest

from mi_agent.policy.store import PolicyStore
from mi_agent.skills.library import SkillLibrary
from mi_agent.skills.model import SkillRecord, SkillStep
from mi_agent.state.store import JSONFileStateStore
from mi_agent.workflow.models import EpisodeAction

pytestmark = pytest.mark.phase3


def test_json_file_state_store_uses_atomic_write_and_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state" / "data.json"
    calls = {"replace": 0, "fsync": 0, "flock": 0}

    real_replace = os.replace
    real_fsync = os.fsync
    real_flock = fcntl.flock

    def replace_spy(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        calls["replace"] += 1
        real_replace(src, dst)

    def fsync_spy(fd: int) -> None:
        calls["fsync"] += 1
        real_fsync(fd)

    def flock_spy(fd: int, op: int) -> None:
        calls["flock"] += 1
        real_flock(fd, op)

    monkeypatch.setattr("mi_agent.state.store.os.replace", replace_spy)
    monkeypatch.setattr("mi_agent.state.store.os.fsync", fsync_spy)
    monkeypatch.setattr("mi_agent.state.store.fcntl.flock", flock_spy)

    store = JSONFileStateStore[dict[str, int]](
        path,
        default_factory=dict,
        decode=lambda raw: {k: int(v) for k, v in dict(raw).items()},
        encode=lambda state: state,
    )
    store.save({"a": 1})

    loaded = store.load()
    assert loaded == {"a": 1}
    assert calls["replace"] >= 1
    assert calls["fsync"] >= 2
    assert calls["flock"] >= 2


def test_policy_store_canonicalization_persists_after_reload(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    store = PolicyStore(path)
    store.update_episode(
        "database_initialization_storage_missing",
        [EpisodeAction(action_key="apply_schema|", reward=1.0, duration_seconds=0.2, success=True)],
    )

    reloaded = PolicyStore(path)
    value = reloaded.value_for("db_file_missing", "apply_schema|")
    assert value.visits == 1


def test_skill_library_migrates_failure_type_on_load(tmp_path: Path) -> None:
    path = tmp_path / "skills.json"
    legacy = SkillRecord(
        name="recover-db-from-schema",
        description="Apply schema",
        failure_type="database_initialization_storage_missing",
        trigger_fingerprint={},
        steps=[SkillStep(primitive_id="apply_schema", parameters={"db_path": "data/app.db"})],
        expected_postcondition="healthy",
    )
    path.write_text(json.dumps([legacy.model_dump(mode="json")]), encoding="utf-8")

    library = SkillLibrary(path)
    skills = library.all(include_inactive=True)

    assert len(skills) == 1
    assert skills[0].failure_type == "db_file_missing"
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted[0]["failure_type"] == "db_file_missing"
