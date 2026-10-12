from __future__ import annotations

from pathlib import Path

import pytest

from mi_agent.llm.client import FakeLLMClient
from mi_agent.skills.embedding import DeterministicEmbedder
from mi_agent.skills.library import SkillLibrary
from mi_agent.skills.matcher import SkillMatcher
from mi_agent.skills.model import SkillRecord, SkillStep

pytestmark = pytest.mark.phase6


def test_deterministic_embedder_is_stable() -> None:
    embedder = DeterministicEmbedder(dimension=16)

    first = embedder.embed("db missing probe signature")
    second = embedder.embed("db missing probe signature")
    third = embedder.embed("service timeout signature")

    assert first == second
    assert first != third


@pytest.mark.asyncio()
async def test_matcher_uses_embedding_similarity_before_llm(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "skills.json")
    library.register(
        SkillRecord(
            name="recover-db",
            description="recover db",
            failure_type="db_file_missing",
            trigger_fingerprint={"probes": "db missing signature", "logs": "open database file"},
            steps=[SkillStep(primitive_id="apply_schema", parameters={"db_path": "data/app.db"})],
            expected_postcondition="healthy",
        )
    )

    llm = FakeLLMClient()
    matcher = SkillMatcher(
        library,
        llm,
        embedder=DeterministicEmbedder(dimension=16),
        similarity_threshold=0.6,
    )

    match = await matcher.match(
        failure_type="db_file_missing",
        trigger_fingerprint={"probes": "db missing signature", "logs": "database open failed"},
    )

    assert match is not None
    assert match.name == "recover-db"
    assert llm.calls["match_skill"] == 0


@pytest.mark.asyncio()
async def test_matcher_falls_back_to_llm_when_similarity_below_threshold(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "skills.json")
    library.register(
        SkillRecord(
            name="recover-db",
            description="recover db",
            failure_type="db_file_missing",
            trigger_fingerprint={"probes": "db missing signature", "logs": "open database file"},
            steps=[SkillStep(primitive_id="apply_schema", parameters={"db_path": "data/app.db"})],
            expected_postcondition="healthy",
        )
    )

    llm = FakeLLMClient()
    llm.queue_response("match_skill", {"skill_name": "recover-db", "confidence": 0.9})
    matcher = SkillMatcher(
        library,
        llm,
        embedder=DeterministicEmbedder(dimension=16),
        similarity_threshold=0.99,
    )

    match = await matcher.match(
        failure_type="db_file_missing",
        trigger_fingerprint={"probes": "disk full signature", "logs": "no space left"},
    )

    assert match is not None
    assert match.name == "recover-db"
    assert llm.calls["match_skill"] == 1
