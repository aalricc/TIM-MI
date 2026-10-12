from __future__ import annotations

import json
from pathlib import Path

import pytest

from mi_agent.skills.library import SkillLibrary
from mi_agent.skills.model import SkillRecord, SkillStep

pytestmark = pytest.mark.phase6


def test_skill_step_resolves_observation_bindings() -> None:
    step = SkillStep(
        primitive_id="set_permissions",
        parameters={"path": "data/app.db", "mode": 0o660},
        observation_bindings={"path": "db_path"},
    )

    resolved = step.resolve_parameters({"db_path": "data/alternate.db"})

    assert resolved["path"] == "data/alternate.db"
    assert resolved["mode"] == 0o660


def test_skill_record_has_provenance_defaults() -> None:
    record = SkillRecord(
        name="recover-db",
        description="Apply schema",
        failure_type="db_file_missing",
        steps=[SkillStep(primitive_id="apply_schema", parameters={"db_path": "data/app.db"})],
        expected_postcondition="healthy",
    )

    assert record.schema_version == "v2"
    assert record.provenance.source == "agent_verified_success"
    assert record.provenance.source_incidents == []


def test_library_register_tracks_provenance_and_versioning(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "skills.json")

    first = library.register(
        SkillRecord(
            name="recover-db",
            description="Apply schema",
            failure_type="db_file_missing",
            steps=[
                SkillStep(
                    primitive_id="apply_schema",
                    parameters={"db_path": "data/app.db", "schema_path": "schema.sql"},
                )
            ],
            expected_postcondition="healthy",
        ),
        source_incident_id="inc-1",
        environment_fingerprint={"probe_fingerprint": "fp-1"},
    )

    second = library.register(
        SkillRecord(
            name="recover-db",
            description="Apply schema",
            failure_type="db_file_missing",
            steps=[
                SkillStep(
                    primitive_id="apply_schema",
                    parameters={"db_path": "data/app.db", "schema_path": "schema.sql"},
                )
            ],
            expected_postcondition="healthy",
        ),
        source_incident_id="inc-2",
        environment_fingerprint={"probe_fingerprint": "fp-2"},
    )

    assert first.version == 1
    assert second.version == 2
    assert second.provenance.source_incidents == ["inc-1", "inc-2"]
    assert second.provenance.environment_fingerprint["probe_fingerprint"] == "fp-2"


def test_library_can_persist_and_reload_markdown_store(tmp_path: Path) -> None:
    path = tmp_path / "skills.md"
    library = SkillLibrary(path)

    library.register(
        SkillRecord(
            name="recover-db",
            description="Apply schema",
            failure_type="db_file_missing",
            steps=[
                SkillStep(
                    primitive_id="apply_schema",
                    parameters={"db_path": "data/app.db", "schema_path": "schema.sql"},
                )
            ],
            expected_postcondition="healthy",
        )
    )

    content = path.read_text(encoding="utf-8")
    assert "# MI Skills Knowledge Base" in content
    assert "<!-- MI_SKILLS_JSON_BEGIN -->" in content
    assert "## recover-db" in content

    reloaded = SkillLibrary(path)
    names = [skill.name for skill in reloaded.all(include_inactive=True)]
    assert names == ["recover-db"]


def test_library_markdown_store_migrates_legacy_json_file(tmp_path: Path) -> None:
    markdown_path = tmp_path / "skills.md"
    legacy_json_path = tmp_path / "skills.json"
    legacy_payload = [
        SkillRecord(
            name="legacy-skill",
            description="legacy",
            failure_type="db_file_missing",
            steps=[SkillStep(primitive_id="ping_db", parameters={})],
            expected_postcondition="healthy",
        ).model_dump(mode="json")
    ]
    legacy_json_path.write_text(json.dumps(legacy_payload), encoding="utf-8")

    library = SkillLibrary(markdown_path)

    assert [skill.name for skill in library.all(include_inactive=True)] == ["legacy-skill"]
    assert markdown_path.exists()
