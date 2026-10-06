from __future__ import annotations

import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from pydantic import TypeAdapter, ValidationError

from mi_agent.errors import CorruptStoreError
from mi_agent.failure_types import normalize_failure_type
from mi_agent.skills.model import SkillRecord, SkillStats


class SkillLibrary:
    def __init__(self, store_path: Path) -> None:
        self._store_path = store_path
        self._skills: dict[str, SkillRecord] = {}
        self._adapter = TypeAdapter(list[SkillRecord])
        self._load()

    def _load(self) -> None:
        if not self._store_path.exists():
            self._skills = {}
            return
        try:
            raw = json.loads(self._store_path.read_text(encoding="utf-8"))
            skills = self._adapter.validate_python(raw)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise CorruptStoreError(f"Corrupt skill store: {self._store_path}") from exc
        canonicalized, canonical_migrated = self._canonicalize_failure_types(skills)
        deduped, dedup_migrated = self._deduplicate_loaded_skills(canonicalized)
        migrated = canonical_migrated or dedup_migrated
        self._skills = deduped
        if migrated:
            self._persist()

    def _persist(self) -> None:
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        payload = [skill.model_dump(mode="json") for skill in self._skills.values()]
        with NamedTemporaryFile(
            "w", delete=False, dir=self._store_path.parent, encoding="utf-8"
        ) as tmp:
            json.dump(payload, tmp, ensure_ascii=False, sort_keys=True)
            temp_path = Path(tmp.name)
        temp_path.replace(self._store_path)

    def register(self, skill: SkillRecord) -> SkillRecord:
        skill.failure_type = normalize_failure_type(skill.failure_type)
        canonical_name = self._find_equivalent_skill_name(skill)
        if canonical_name is not None:
            existing = self._skills[canonical_name]
            updated = skill.model_copy(deep=True)
            updated.name = canonical_name
            updated.version = existing.version + 1
            updated.active = True
            updated.stats = existing.stats.model_copy(deep=True)
            updated.created_at = existing.created_at
            self._skills[canonical_name] = updated
            self._persist()
            return updated

        existing_by_name = self._skills.get(skill.name)
        if existing_by_name is not None:
            skill.version = existing_by_name.version + 1
            skill.stats = existing_by_name.stats.model_copy(deep=True)
            if not existing_by_name.active:
                skill.active = False
            skill.created_at = existing_by_name.created_at

        self._skills[skill.name] = skill
        self._persist()
        return skill

    def all(
        self, failure_type: str | None = None, *, include_inactive: bool = False
    ) -> list[SkillRecord]:
        values = list(self._skills.values())
        if failure_type is not None:
            failure_type = normalize_failure_type(failure_type)
            values = [s for s in values if s.failure_type == failure_type]
        if not include_inactive:
            values = [s for s in values if s.active]
        return sorted(values, key=lambda s: s.created_at)

    def get(self, name: str) -> SkillRecord | None:
        return self._skills.get(name)

    def record_result(self, name: str, *, success: bool, recovery_seconds: float) -> None:
        skill = self._skills[name]
        stats = skill.stats
        if success:
            total = stats.success_count + 1
            stats.avg_recovery_seconds = (
                (stats.avg_recovery_seconds * stats.success_count) + recovery_seconds
            ) / total
            stats.success_count = total
        else:
            stats.failure_count += 1
            if stats.failure_count >= 3 and stats.success_count == 0:
                skill.active = False
        self._persist()

    def retire_if_repeated_failures(self, name: str) -> None:
        skill = self._skills[name]
        if skill.stats.failure_count >= 5 and skill.stats.failure_count > skill.stats.success_count:
            skill.active = False
            self._persist()

    def _find_equivalent_skill_name(self, candidate: SkillRecord) -> str | None:
        candidate_signature = self._skill_signature(candidate)
        for name, existing in self._skills.items():
            if existing.failure_type != candidate.failure_type:
                continue
            if self._skill_signature(existing) == candidate_signature:
                return name
        return None

    @classmethod
    def _skill_signature(
        cls,
        skill: SkillRecord,
    ) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
        return tuple(
            (step.primitive_id, cls._stable_params(step.parameters))
            for step in skill.steps
        )

    @staticmethod
    def _stable_params(parameters: dict[str, Any]) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((key, repr(value)) for key, value in parameters.items()))

    @classmethod
    def _deduplicate_loaded_skills(
        cls,
        skills: list[SkillRecord],
    ) -> tuple[dict[str, SkillRecord], bool]:
        groups: dict[
            tuple[str, tuple[tuple[str, tuple[tuple[str, str], ...]], ...]],
            list[SkillRecord],
        ] = {}
        for skill in sorted(skills, key=lambda item: (item.created_at, item.name)):
            signature = (skill.failure_type, cls._skill_signature(skill))
            groups.setdefault(signature, []).append(skill)

        deduped: dict[str, SkillRecord] = {}
        migrated = False
        for grouped in groups.values():
            collapsed = cls._collapse_skill_group(grouped)
            if len(grouped) > 1:
                migrated = True
            existing = deduped.get(collapsed.name)
            if existing is None:
                deduped[collapsed.name] = collapsed
                continue
            migrated = True
            if collapsed.version > existing.version:
                deduped[collapsed.name] = collapsed

        if len(deduped) != len(skills):
            migrated = True
        return deduped, migrated

    @classmethod
    def _collapse_skill_group(cls, grouped: list[SkillRecord]) -> SkillRecord:
        if len(grouped) == 1:
            return grouped[0]
        canonical = grouped[0].model_copy(deep=True)
        canonical.version = max(skill.version for skill in grouped)
        canonical.active = any(skill.active for skill in grouped)
        canonical.stats = cls._aggregate_stats(grouped)
        return canonical

    @staticmethod
    def _aggregate_stats(grouped: list[SkillRecord]) -> SkillStats:
        success_count = sum(skill.stats.success_count for skill in grouped)
        failure_count = sum(skill.stats.failure_count for skill in grouped)
        weighted_recovery = sum(
            skill.stats.avg_recovery_seconds * skill.stats.success_count for skill in grouped
        )
        avg_recovery_seconds = (weighted_recovery / success_count) if success_count > 0 else 0.0
        return SkillStats(
            success_count=success_count,
            failure_count=failure_count,
            avg_recovery_seconds=avg_recovery_seconds,
        )

    @staticmethod
    def _canonicalize_failure_types(skills: list[SkillRecord]) -> tuple[list[SkillRecord], bool]:
        migrated = False
        normalized: list[SkillRecord] = []
        for skill in skills:
            canonical_type = normalize_failure_type(skill.failure_type)
            if canonical_type == skill.failure_type:
                normalized.append(skill)
                continue
            updated = skill.model_copy(deep=True)
            updated.failure_type = canonical_type
            normalized.append(updated)
            migrated = True
        return normalized, migrated
