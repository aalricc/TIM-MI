from __future__ import annotations

from pathlib import Path

import pytest

from mi_agent.skills.library import SkillLibrary
from mi_agent.skills.model import SkillRecord, SkillStep

pytestmark = pytest.mark.phase6


def _skill(name: str = "recover-db") -> SkillRecord:
    return SkillRecord(
        name=name,
        description="Apply schema",
        failure_type="db_file_missing",
        trigger_fingerprint={"probes": "missing"},
        steps=[SkillStep(primitive_id="apply_schema", parameters={"db_path": "data/app.db"})],
        expected_postcondition="healthy",
    )


def test_experience_update_add_upvote_downvote(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "skills.json")

    added = library.apply_experience_update(operation="add", skill=_skill())
    assert added.name == "recover-db"

    library.apply_experience_update(operation="upvote", name="recover-db", recovery_seconds=2.0)
    library.apply_experience_update(operation="downvote", name="recover-db")

    stored = library.get("recover-db")
    assert stored is not None
    assert stored.stats.success_count == 1
    assert stored.stats.failure_count == 1
    assert stored.stats.avg_recovery_seconds == pytest.approx(2.0)


def test_experience_update_edit_preserves_stats_and_increments_version(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "skills.json")

    original = library.apply_experience_update(operation="add", skill=_skill())
    library.apply_experience_update(operation="upvote", name=original.name, recovery_seconds=1.5)

    edited = library.apply_experience_update(
        operation="edit",
        skill=SkillRecord(
            name=original.name,
            description="Apply schema then checkpoint",
            failure_type="db_file_missing",
            trigger_fingerprint={"probes": "missing"},
            steps=[
                SkillStep(
                    primitive_id="apply_schema",
                    parameters={"db_path": "data/app.db", "schema_path": "schema.sql"},
                ),
                SkillStep(
                    primitive_id="checkpoint_wal",
                    parameters={"db_path": "data/app.db"},
                ),
            ],
            expected_postcondition="healthy",
        ),
    )

    assert edited.version == original.version + 1
    assert edited.stats.success_count == 1
    assert edited.description == "Apply schema then checkpoint"
    assert [step.primitive_id for step in edited.steps] == ["apply_schema", "checkpoint_wal"]

