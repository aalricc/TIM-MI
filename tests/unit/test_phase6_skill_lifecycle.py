from __future__ import annotations

from pathlib import Path

import pytest

from mi_agent.skills.library import SkillLibrary
from mi_agent.skills.model import SkillRecord, SkillStep

pytestmark = pytest.mark.phase6


def _skill() -> SkillRecord:
    return SkillRecord(
        name="recover-db",
        description="recover db",
        failure_type="db_file_missing",
        trigger_fingerprint={"a": "b"},
        steps=[SkillStep(primitive_id="apply_schema", parameters={"db_path": "data/app.db"})],
        expected_postcondition="healthy",
    )


def test_skill_lifecycle_promotes_candidate_to_canary_to_trusted(tmp_path: Path) -> None:
    library = SkillLibrary(
        tmp_path / "skills.json",
        candidate_to_canary_successes=1,
        canary_to_trusted_successes=2,
        trusted_regression_failures=2,
    )
    library.register(_skill())

    first = library.get("recover-db")
    assert first is not None
    assert first.lifecycle_state == "candidate"

    library.record_result("recover-db", success=True, recovery_seconds=1.0)
    second = library.get("recover-db")
    assert second is not None
    assert second.lifecycle_state == "canary"

    library.record_result("recover-db", success=True, recovery_seconds=1.0)
    third = library.get("recover-db")
    assert third is not None
    assert third.lifecycle_state == "trusted"


def test_skill_lifecycle_demotes_trusted_on_regression(tmp_path: Path) -> None:
    library = SkillLibrary(
        tmp_path / "skills.json",
        candidate_to_canary_successes=1,
        canary_to_trusted_successes=1,
        trusted_regression_failures=2,
    )
    library.register(_skill())
    library.record_result("recover-db", success=True, recovery_seconds=1.0)
    library.record_result("recover-db", success=True, recovery_seconds=1.0)

    pre = library.get("recover-db")
    assert pre is not None
    assert pre.lifecycle_state == "trusted"

    library.record_result("recover-db", success=False, recovery_seconds=1.0)
    library.record_result("recover-db", success=False, recovery_seconds=1.0)

    post = library.get("recover-db")
    assert post is not None
    assert post.lifecycle_state == "deprecated"
    assert post.active is False

