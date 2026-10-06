from __future__ import annotations

import json
from pathlib import Path

import pytest

from mi_agent.errors import (
    CorruptStoreError,
    LLMOutputValidationError,
    PathScopeError,
    ToolParameterValidationError,
)
from mi_agent.events.bus import EventBus
from mi_agent.llm.client import FakeLLMClient
from mi_agent.skills.distiller import SkillDistiller
from mi_agent.skills.library import SkillLibrary
from mi_agent.skills.matcher import SkillMatcher
from mi_agent.skills.model import SkillRecord, SkillStep
from mi_agent.tools.executor import ToolExecutor


@pytest.mark.asyncio()
async def test_skill_validation_rejects_non_allowlisted_step(test_settings) -> None:
    llm = FakeLLMClient()
    llm.queue_response(
        "distill_skill",
        {
            "name": "recover-db",
            "description": "recover database",
            "failure_type": "db_file_missing",
            "trigger_fingerprint": {"k": "v"},
            "steps": [{"action_id": "unknown_tool", "parameters": {}}],
            "expected_postcondition": "healthy",
        },
    )

    distiller = SkillDistiller(llm, ToolExecutor(test_settings, EventBus()))
    with pytest.raises(LLMOutputValidationError):
        await distiller.distill(
            failure_type="db_file_missing",
            episode_actions=[("create_empty_sqlite_db", {"path": "data/app.db"}, True)],
            trigger_fingerprint={"k": "v"},
            allowed_primitives=frozenset({"create_empty_sqlite_db"}),
        )


@pytest.mark.asyncio()
async def test_skill_validation_rejects_bad_parameters_and_path_escape(test_settings) -> None:
    llm = FakeLLMClient()
    llm.queue_response(
        "distill_skill",
        {
            "name": "bad-skill",
            "description": "bad skill",
            "failure_type": "db_file_missing",
            "trigger_fingerprint": {"k": "v"},
            "steps": [
                {"action_id": "set_permissions", "parameters": {"path": "../../etc", "mode": 384}}
            ],
            "expected_postcondition": "healthy",
        },
    )

    distiller = SkillDistiller(llm, ToolExecutor(test_settings, EventBus()))
    with pytest.raises((ToolParameterValidationError, PathScopeError)):
        await distiller.distill(
            failure_type="db_file_missing",
            episode_actions=[("set_permissions", {"path": "data/app.db", "mode": 384}, True)],
            trigger_fingerprint={"k": "v"},
            allowed_primitives=frozenset({"set_permissions"}),
        )


@pytest.mark.asyncio()
async def test_skill_distillation_applies_default_parameters_before_validation(
    test_settings,
) -> None:
    llm = FakeLLMClient()
    llm.queue_response(
        "distill_skill",
        {
            "name": "recover-db-defaulted",
            "description": "Recover DB with schema defaults",
            "failure_type": "db_file_missing",
            "trigger_fingerprint": {"k": "v"},
            "steps": [{"action_id": "apply_schema", "parameters": {}}],
            "expected_postcondition": "healthy",
        },
    )

    distiller = SkillDistiller(llm, ToolExecutor(test_settings, EventBus()))
    skill = await distiller.distill(
        failure_type="db_file_missing",
        episode_actions=[
            ("apply_schema", {"db_path": "data/app.db", "schema_path": "schema.sql"}, True)
        ],
        trigger_fingerprint={"k": "v"},
        allowed_primitives=frozenset({"apply_schema"}),
    )

    assert skill is not None
    assert skill.steps[0].parameters == {"db_path": "data/app.db", "schema_path": "schema.sql"}


@pytest.mark.asyncio()
async def test_skill_matching_exact_then_llm_fallback(test_settings, tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "skills.json")
    skill = SkillRecord(
        name="recover-db",
        description="recover db",
        failure_type="db_file_missing",
        trigger_fingerprint={"probes": "x", "logs": "y"},
        steps=[
            SkillStep(primitive_id="create_empty_sqlite_db", parameters={"path": "data/app.db"})
        ],
        expected_postcondition="healthy",
    )
    library.register(skill)

    llm = FakeLLMClient()
    matcher = SkillMatcher(library, llm)

    match = await matcher.match(
        failure_type="db_file_missing",
        trigger_fingerprint={"probes": "x", "logs": "y"},
    )
    assert match is not None
    assert match.name == "recover-db"
    assert llm.calls["match_skill"] == 0

    llm.queue_response("match_skill", {"skill_name": "recover-db", "confidence": 0.9})
    match2 = await matcher.match(
        failure_type="db_file_missing",
        trigger_fingerprint={"probes": "other", "logs": "other"},
    )
    assert match2 is not None
    assert llm.calls["match_skill"] == 1


def test_skill_demotion_and_retirement(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "skills.json")
    skill = SkillRecord(
        name="recover-db",
        description="recover db",
        failure_type="db_file_missing",
        trigger_fingerprint={"a": "b"},
        steps=[
            SkillStep(primitive_id="create_empty_sqlite_db", parameters={"path": "data/app.db"})
        ],
        expected_postcondition="healthy",
    )
    library.register(skill)

    for _ in range(5):
        library.record_result("recover-db", success=False, recovery_seconds=1.0)
    library.retire_if_repeated_failures("recover-db")

    retired = library.get("recover-db")
    assert retired is not None
    assert retired.active is False


def test_skill_library_dedups_equivalent_steps_across_names(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "skills.json")

    first = SkillRecord(
        name="recover-db-schema-a",
        description="recover db",
        failure_type="db_file_missing",
        trigger_fingerprint={"a": "b"},
        steps=[
            SkillStep(
                primitive_id="apply_schema",
                parameters={"db_path": "data/app.db", "schema_path": "schema.sql"},
            )
        ],
        expected_postcondition="healthy",
    )
    library.register(first)
    library.record_result("recover-db-schema-a", success=True, recovery_seconds=1.2)

    second = SkillRecord(
        name="recover-db-schema-b",
        description="same steps new wording",
        failure_type="db_file_missing",
        trigger_fingerprint={"x": "y"},
        steps=[
            SkillStep(
                primitive_id="apply_schema",
                parameters={"schema_path": "schema.sql", "db_path": "data/app.db"},
            )
        ],
        expected_postcondition="healthy",
    )
    merged = library.register(second)

    assert merged.name == "recover-db-schema-a"
    assert merged.version == 2
    assert merged.stats.success_count == 1
    assert library.get("recover-db-schema-b") is None


def test_corrupt_skill_library_raises(tmp_path: Path) -> None:
    path = tmp_path / "skills.json"
    path.write_text("[not-valid", encoding="utf-8")
    with pytest.raises(CorruptStoreError):
        SkillLibrary(path)


def test_skill_library_load_migrates_equivalent_duplicate_skills(tmp_path: Path) -> None:
    path = tmp_path / "skills.json"

    first = SkillRecord(
        name="recover-db-a",
        version=1,
        description="first",
        failure_type="db_file_missing",
        trigger_fingerprint={"one": "1"},
        steps=[
            SkillStep(
                primitive_id="apply_schema",
                parameters={"db_path": "data/app.db", "schema_path": "schema.sql"},
            )
        ],
        expected_postcondition="healthy",
    )
    first.stats.success_count = 2
    first.stats.failure_count = 1
    first.stats.avg_recovery_seconds = 5.0

    second = SkillRecord(
        name="recover-db-b",
        version=3,
        description="second",
        failure_type="db_file_missing",
        trigger_fingerprint={"two": "2"},
        steps=[
            SkillStep(
                primitive_id="apply_schema",
                parameters={"schema_path": "schema.sql", "db_path": "data/app.db"},
            )
        ],
        expected_postcondition="healthy",
    )
    second.stats.success_count = 1
    second.stats.failure_count = 0
    second.stats.avg_recovery_seconds = 11.0

    path.write_text(
        json.dumps([first.model_dump(mode="json"), second.model_dump(mode="json")]),
        encoding="utf-8",
    )

    library = SkillLibrary(path)
    active_skills = library.all("db_file_missing", include_inactive=True)

    assert len(active_skills) == 1
    migrated = active_skills[0]
    assert migrated.name == "recover-db-a"
    assert migrated.version == 3
    assert migrated.stats.success_count == 3
    assert migrated.stats.failure_count == 1
    assert migrated.stats.avg_recovery_seconds == pytest.approx(7.0)

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert len(persisted) == 1


def test_skill_library_load_canonicalizes_failure_type(tmp_path: Path) -> None:
    path = tmp_path / "skills.json"
    skill = SkillRecord(
        name="recover-db-storage",
        description="recover db storage issue",
        failure_type="database_initialization_storage_missing",
        trigger_fingerprint={"p": "x"},
        steps=[
            SkillStep(
                primitive_id="apply_schema",
                parameters={"db_path": "data/app.db", "schema_path": "schema.sql"},
            )
        ],
        expected_postcondition="healthy",
    )
    path.write_text(json.dumps([skill.model_dump(mode="json")]), encoding="utf-8")

    library = SkillLibrary(path)

    canonical = library.all("db_file_missing", include_inactive=True)
    assert len(canonical) == 1
    assert canonical[0].failure_type == "db_file_missing"
    assert library.all("database_initialization_storage_missing", include_inactive=True)
