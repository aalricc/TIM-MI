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
        steps=[
            SkillStep(
                primitive_id="apply_schema",
                parameters={"db_path": "data/app.db", "schema_path": "schema.sql"},
            )
        ],
        expected_postcondition="healthy",
    )


def test_skill_checkpoint_export_includes_provenance_and_environment_fingerprint(
    tmp_path: Path,
) -> None:
    library = SkillLibrary(tmp_path / "skills.json")
    library.register(
        _skill(),
        source_incident_id="incident-123",
        environment_fingerprint={"probes": "fingerprint-a", "logs": "fingerprint-b"},
    )

    exported = library.export_checkpoint()

    assert exported["schema_version"] == "v2-skill-checkpoint"
    assert exported["skills"][0]["provenance"]["source_incidents"] == ["incident-123"]
    assert exported["skills"][0]["provenance"]["environment_fingerprint"] == {
        "probes": "fingerprint-a",
        "logs": "fingerprint-b",
    }


def test_imported_checkpoint_skills_always_enter_candidate_state(tmp_path: Path) -> None:
    source_library = SkillLibrary(tmp_path / "source-skills.json")
    trusted = _skill()
    trusted.lifecycle_state = "trusted"
    trusted.stats.success_count = 8
    source_library.register(trusted)

    exported = source_library.export_checkpoint()

    target_library = SkillLibrary(tmp_path / "target-skills.json")
    imported = target_library.import_checkpoint(exported)

    assert len(imported) == 1
    imported_skill = target_library.get("recover-db")
    assert imported_skill is not None
    assert imported_skill.lifecycle_state == "candidate"
    assert imported_skill.active is True
    assert imported_skill.provenance.source == "imported_checkpoint"

