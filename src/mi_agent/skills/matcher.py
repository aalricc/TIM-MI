from __future__ import annotations

from dataclasses import dataclass

from mi_agent.llm.client import LLMClient
from mi_agent.skills.embedding import (
    DeterministicEmbedder,
    Embedder,
    cosine_similarity,
    fingerprint_text,
)
from mi_agent.skills.library import SkillLibrary
from mi_agent.skills.model import SkillRecord


@dataclass(frozen=True, slots=True)
class SimilarityMatch:
    skill: SkillRecord
    similarity: float


class SkillMatcher:
    def __init__(
        self,
        library: SkillLibrary,
        llm_client: LLMClient,
        *,
        embedder: Embedder | None = None,
        similarity_threshold: float = 0.8,
    ) -> None:
        self._library = library
        self._llm_client = llm_client
        self._embedder = embedder or DeterministicEmbedder()
        self._similarity_threshold = max(0.0, min(1.0, similarity_threshold))

    async def match(
        self,
        *,
        failure_type: str,
        trigger_fingerprint: dict[str, str],
    ) -> SkillRecord | None:
        candidates = self._library.all(failure_type)
        exact = self._exact_match(candidates, trigger_fingerprint)
        if exact is not None:
            return exact
        if not candidates:
            return None
        nearest = self._nearest_by_similarity(candidates, trigger_fingerprint)
        if nearest is not None and nearest.similarity >= self._similarity_threshold:
            return nearest.skill
        llm_match = await self._llm_client.match_skill(
            failure_type=failure_type,
            trigger_fingerprint=trigger_fingerprint,
            candidate_skills=[skill.name for skill in candidates],
        )
        if llm_match.skill_name is None or llm_match.confidence < 0.8:
            return None
        return self._library.get(llm_match.skill_name)

    @staticmethod
    def _exact_match(
        candidates: list[SkillRecord],
        trigger_fingerprint: dict[str, str],
    ) -> SkillRecord | None:
        for skill in candidates:
            if skill.trigger_fingerprint == trigger_fingerprint:
                return skill
        return None

    def _nearest_by_similarity(
        self,
        candidates: list[SkillRecord],
        trigger_fingerprint: dict[str, str],
    ) -> SimilarityMatch | None:
        query_text = fingerprint_text(trigger_fingerprint)
        query_vector = self._embedder.embed(query_text)

        best_skill: SkillRecord | None = None
        best_similarity = -1.0
        for skill in candidates:
            skill_text = fingerprint_text(skill.trigger_fingerprint)
            skill_vector = self._embedder.embed(skill_text)
            similarity = cosine_similarity(query_vector, skill_vector)
            if similarity > best_similarity:
                best_similarity = similarity
                best_skill = skill

        if best_skill is None:
            return None
        return SimilarityMatch(skill=best_skill, similarity=best_similarity)
