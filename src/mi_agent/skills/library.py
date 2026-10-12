from __future__ import annotations

import fcntl
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal

from pydantic import TypeAdapter, ValidationError

from mi_agent.errors import CorruptStoreError
from mi_agent.failure_types import normalize_failure_type
from mi_agent.skills.model import SkillRecord, SkillStats
from mi_agent.state.store import JSONFileStateStore


class SkillLibrary:
    _JSON_BLOCK_BEGIN = "<!-- MI_SKILLS_JSON_BEGIN -->"
    _JSON_BLOCK_END = "<!-- MI_SKILLS_JSON_END -->"

    def __init__(
        self,
        store_path: Path,
        *,
        candidate_to_canary_successes: int = 2,
        canary_to_trusted_successes: int = 5,
        trusted_regression_failures: int = 3,
    ) -> None:
        self._store_path = store_path
        self._use_markdown_store = store_path.suffix.lower() == ".md"
        self._candidate_to_canary_successes = max(1, candidate_to_canary_successes)
        self._canary_to_trusted_successes = max(1, canary_to_trusted_successes)
        self._trusted_regression_failures = max(1, trusted_regression_failures)
        self._skills: dict[str, SkillRecord] = {}
        self._adapter = TypeAdapter(list[SkillRecord])
        self._state_store: JSONFileStateStore[list[SkillRecord]] | None
        if self._use_markdown_store:
            self._state_store = None
        else:
            self._state_store = JSONFileStateStore[list[SkillRecord]](
                store_path,
                default_factory=list,
                decode=self._decode_skills,
                encode=self._encode_skills,
            )
        self._load()

    def _load(self) -> None:
        try:
            if self._use_markdown_store:
                skills = self._load_markdown_store()
            else:
                state_store = self._state_store
                skills = [] if state_store is None else state_store.load()
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise CorruptStoreError(f"Corrupt skill store: {self._store_path}") from exc
        canonicalized, canonical_migrated = self._canonicalize_failure_types(skills)
        deduped, dedup_migrated = self._deduplicate_loaded_skills(canonicalized)
        migrated = canonical_migrated or dedup_migrated
        self._skills = deduped
        if migrated:
            self._persist()

    def _persist(self) -> None:
        payload = list(self._skills.values())
        if self._use_markdown_store:
            self._save_markdown_store(payload)
            return
        state_store = self._state_store
        if state_store is not None:
            state_store.save(payload)

    def _decode_skills(self, raw: object) -> list[SkillRecord]:
        return self._adapter.validate_python(raw)

    @staticmethod
    def _encode_skills(skills: list[SkillRecord]) -> object:
        return [skill.model_dump(mode="json") for skill in skills]

    def _load_markdown_store(self) -> list[SkillRecord]:
        if self._store_path.exists():
            text = self._store_path.read_text(encoding="utf-8")
            payload = self._extract_json_payload_from_markdown(text)
            return self._decode_skills(payload)

        legacy_json = self._store_path.with_suffix(".json")
        if not legacy_json.exists():
            return []

        payload = json.loads(legacy_json.read_text(encoding="utf-8"))
        skills = self._decode_skills(payload)
        self._save_markdown_store(skills)
        return skills

    def _save_markdown_store(self, skills: list[SkillRecord]) -> None:
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        rendered = self._render_markdown(skills)
        lock_path = self._store_path.with_suffix(f"{self._store_path.suffix}.lock")

        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                with NamedTemporaryFile(
                    mode="w",
                    delete=False,
                    dir=self._store_path.parent,
                    encoding="utf-8",
                ) as handle:
                    handle.write(rendered)
                    handle.flush()
                    os.fsync(handle.fileno())
                    temp_path = Path(handle.name)
                os.replace(temp_path, self._store_path)
                self._fsync_dir(self._store_path.parent)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _render_markdown(self, skills: list[SkillRecord]) -> str:
        ordered = sorted(skills, key=lambda item: (item.created_at, item.name))
        lines = [
            "# MI Skills Knowledge Base",
            "",
            "This file is auto-generated from successful recoveries.",
            f"Updated: {datetime.now(tz=UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "",
        ]
        if not ordered:
            lines.append("_No learned skills yet._")
            lines.append("")
        else:
            for skill in ordered:
                lines.extend(
                    [
                        f"## {skill.name}",
                        f"- failure_type: `{skill.failure_type}`",
                        f"- lifecycle_state: `{skill.lifecycle_state}`",
                        f"- active: `{skill.active}`",
                        f"- version: `{skill.version}`",
                        (
                            "- stats: "
                            f"success={skill.stats.success_count}, "
                            f"failure={skill.stats.failure_count}, "
                            f"avg_recovery_seconds={skill.stats.avg_recovery_seconds:.3f}"
                        ),
                        "- steps:",
                    ]
                )
                for index, step in enumerate(skill.steps, start=1):
                    params = ", ".join(
                        f"{key}={value!r}" for key, value in sorted(step.parameters.items())
                    )
                    lines.append(f"  - {index}. `{step.primitive_id}` ({params})")
                lines.append("")

        lines.extend([self._JSON_BLOCK_BEGIN, "```json"])
        lines.append(
            json.dumps(
                self._encode_skills(ordered),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        lines.extend(["```", self._JSON_BLOCK_END, ""])
        return "\n".join(lines).rstrip() + "\n"

    @classmethod
    def _extract_json_payload_from_markdown(cls, text: str) -> object:
        if text.strip() == "":
            return []

        begin_index = text.find(cls._JSON_BLOCK_BEGIN)
        end_index = text.find(cls._JSON_BLOCK_END)
        if begin_index == -1 or end_index == -1 or end_index <= begin_index:
            fallback = json.loads(text)
            if not isinstance(fallback, list):
                raise ValueError("Markdown skill store must embed a JSON list payload")
            return fallback

        block = text[begin_index + len(cls._JSON_BLOCK_BEGIN) : end_index].strip()
        if block.startswith("```"):
            lines = block.splitlines()
            if lines:
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            block = "\n".join(lines).strip()
        payload = json.loads(block)
        if not isinstance(payload, list):
            raise ValueError("Markdown skill store payload must be a JSON list")
        return payload

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def register(
        self,
        skill: SkillRecord,
        *,
        source_incident_id: str | None = None,
        environment_fingerprint: dict[str, str] | None = None,
    ) -> SkillRecord:
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
            updated.provenance = existing.provenance.model_copy(deep=True)
            self._apply_provenance(
                updated,
                source_incident_id=source_incident_id,
                environment_fingerprint=environment_fingerprint,
            )
            self._skills[canonical_name] = updated
            self._persist()
            return updated

        existing_by_name = self._skills.get(skill.name)
        if existing_by_name is not None:
            skill.version = existing_by_name.version + 1
            skill.stats = existing_by_name.stats.model_copy(deep=True)
            skill.provenance = existing_by_name.provenance.model_copy(deep=True)
            if not existing_by_name.active:
                skill.active = False
            skill.created_at = existing_by_name.created_at

        self._apply_provenance(
            skill,
            source_incident_id=source_incident_id,
            environment_fingerprint=environment_fingerprint,
        )

        self._skills[skill.name] = skill
        self._persist()
        return skill

    @staticmethod
    def _apply_provenance(
        skill: SkillRecord,
        *,
        source_incident_id: str | None,
        environment_fingerprint: dict[str, str] | None,
    ) -> None:
        if (
            source_incident_id is not None
            and source_incident_id not in skill.provenance.source_incidents
        ):
            skill.provenance.source_incidents.append(source_incident_id)
        if environment_fingerprint is not None:
            skill.provenance.environment_fingerprint = dict(environment_fingerprint)

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

    def apply_experience_update(
        self,
        *,
        operation: Literal["add", "upvote", "downvote", "edit"],
        skill: SkillRecord | None = None,
        name: str | None = None,
        recovery_seconds: float = 1.0,
        source_incident_id: str | None = None,
        environment_fingerprint: dict[str, str] | None = None,
    ) -> SkillRecord:
        if operation == "add":
            if skill is None:
                raise ValueError("skill is required for add updates")
            return self.register(
                skill,
                source_incident_id=source_incident_id,
                environment_fingerprint=environment_fingerprint,
            )

        if operation == "edit":
            if skill is None:
                raise ValueError("skill is required for edit updates")
            if skill.name not in self._skills:
                raise KeyError(skill.name)
            return self.register(
                skill,
                source_incident_id=source_incident_id,
                environment_fingerprint=environment_fingerprint,
            )

        target_name = name or (skill.name if skill is not None else None)
        if target_name is None:
            raise ValueError("name is required for vote updates")
        if target_name not in self._skills:
            raise KeyError(target_name)

        if operation == "upvote":
            self.record_result(
                target_name,
                success=True,
                recovery_seconds=max(0.0, recovery_seconds),
            )
        elif operation == "downvote":
            self.record_result(target_name, success=False, recovery_seconds=0.0)
        else:
            raise ValueError(f"Unknown experience update operation: {operation}")

        updated = self._skills.get(target_name)
        if updated is None:
            raise KeyError(target_name)
        return updated

    def export_checkpoint(self, destination: Path | None = None) -> dict[str, Any]:
        payload = {
            "schema_version": "v2-skill-checkpoint",
            "exported_at": datetime.now(tz=UTC).isoformat(),
            "skills": self._encode_skills(list(self._skills.values())),
        }
        if destination is not None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def import_checkpoint(self, checkpoint: dict[str, Any] | Path) -> list[SkillRecord]:
        payload: dict[str, Any]
        if isinstance(checkpoint, Path):
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        else:
            payload = checkpoint

        skills_raw = payload.get("skills") if isinstance(payload, dict) else None
        if not isinstance(skills_raw, list):
            raise ValueError("Checkpoint payload must contain a 'skills' list")

        skills = self._adapter.validate_python(skills_raw)
        imported: list[SkillRecord] = []
        for skill in skills:
            candidate = skill.model_copy(deep=True)
            candidate.lifecycle_state = "candidate"
            candidate.active = True
            candidate.provenance.source = "imported_checkpoint"
            imported.append(self.register(candidate))
        return imported

    def record_result(self, name: str, *, success: bool, recovery_seconds: float) -> None:
        skill = self._skills[name]
        stats = skill.stats
        if success:
            total = stats.success_count + 1
            stats.avg_recovery_seconds = (
                (stats.avg_recovery_seconds * stats.success_count) + recovery_seconds
            ) / total
            stats.success_count = total
            stats.consecutive_failures = 0
        else:
            stats.failure_count += 1
            stats.consecutive_failures += 1
            if stats.failure_count >= 3 and stats.success_count == 0:
                skill.active = False
                skill.lifecycle_state = "deprecated"

        self._apply_lifecycle(skill)
        self._persist()

    def retire_if_repeated_failures(self, name: str) -> None:
        skill = self._skills[name]
        if skill.stats.failure_count >= 5 and skill.stats.failure_count > skill.stats.success_count:
            skill.active = False
            skill.lifecycle_state = "deprecated"
            self._persist()

    def _apply_lifecycle(self, skill: SkillRecord) -> None:
        if skill.lifecycle_state == "deprecated":
            skill.active = False
            return

        if (
            skill.lifecycle_state == "trusted"
            and skill.stats.consecutive_failures >= self._trusted_regression_failures
        ):
            skill.lifecycle_state = "deprecated"
            skill.active = False
            return

        if (
            skill.lifecycle_state == "canary"
            and skill.stats.success_count >= self._canary_to_trusted_successes
        ):
            skill.lifecycle_state = "trusted"
            skill.active = True
            return

        if (
            skill.lifecycle_state == "candidate"
            and skill.stats.success_count >= self._candidate_to_canary_successes
        ):
            skill.lifecycle_state = "canary"
            skill.active = True

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
