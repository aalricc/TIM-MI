from __future__ import annotations

from mi_agent.llm.client import LLMClient
from mi_agent.skills.library import SkillLibrary
from mi_agent.skills.model import SkillRecord


class SkillMatcher:
    def __init__(self, library: SkillLibrary, llm_client: LLMClient) -> None:
        self._library = library
        self._llm_client = llm_client

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
