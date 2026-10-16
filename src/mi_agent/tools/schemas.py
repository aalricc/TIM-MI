from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class EmptyParams(BaseModel):
    pass


class ReadAppLogParams(BaseModel):
    lines: int = Field(default=100, ge=1, le=2000)


class ListDirectoryParams(BaseModel):
    path: str = "."


class StatPathParams(BaseModel):
    path: str


class ReadAppFileParams(BaseModel):
    path: str


class CreateDirectoryParams(BaseModel):
    path: str


class CreateEmptySQLiteDBParams(BaseModel):
    path: str = "data/app.db"


class ApplySchemaParams(BaseModel):
    db_path: str = "data/app.db"
    schema_path: str = "schema.sql"


class CheckDBIntegrityParams(BaseModel):
    db_path: str = "data/app.db"


class ProbeDBRWParams(BaseModel):
    db_path: str = "data/app.db"


class InspectRuntimeConfigParams(BaseModel):
    pass


class HealthDeepProbeParams(BaseModel):
    pass


class InspectPathsParams(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=100)


class ListBackupsParams(BaseModel):
    limit: int = Field(default=50, ge=1, le=200)


class WaitParams(BaseModel):
    seconds: float = Field(default=1.0, ge=0.0, le=30.0)


class ReindexDBParams(BaseModel):
    db_path: str = "data/app.db"


class CheckpointWALParams(BaseModel):
    db_path: str = "data/app.db"


class RecoverDBToNewFileParams(BaseModel):
    source_db_path: str = "data/app.db"
    output_path: str


class MovePathParams(BaseModel):
    source_path: str
    destination_path: str
    overwrite: bool = False


class WriteAppFileParams(BaseModel):
    path: str
    content: str = Field(max_length=200_000)


class RemovePathParams(BaseModel):
    path: str
    recursive: bool = False


class RequestBackupRestoreParams(BaseModel):
    backup_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,120}$")


class SetPermissionsParams(BaseModel):
    path: str
    mode: Literal[0o600, 0o640, 0o660, 0o700, 0o750]


class RestartServiceParams(BaseModel):
    service_name: str = "target-app"


class ActionChoice(BaseModel):
    action_id: str
    parameters: dict[str, object] = Field(default_factory=dict)
    preconditions: list[str] = Field(default_factory=list, max_length=10)
    expected_postconditions: list[str] = Field(default_factory=list, max_length=10)


class CandidatePlan(BaseModel):
    name: str
    rationale: str
    steps: list[ActionChoice] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def _ensure_loop_free(self) -> CandidatePlan:
        seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
        for step in self.steps:
            signature = (
                step.action_id,
                tuple(sorted((key, repr(value)) for key, value in step.parameters.items())),
            )
            if signature in seen:
                raise ValueError("Candidate plan must be loop-free")
            seen.add(signature)
        return self


class SkillProposal(BaseModel):
    name: str = Field(min_length=3, max_length=80)
    description: str = Field(min_length=8, max_length=300)
    failure_type: str
    trigger_fingerprint: dict[str, str] = Field(default_factory=dict)
    preconditions: list[str] = Field(default_factory=list, max_length=10)
    steps: list[ActionChoice] = Field(min_length=1, max_length=15)
    expected_postcondition: str = Field(min_length=3, max_length=200)


class MatchSkillOutput(BaseModel):
    skill_name: str | None
    confidence: float = Field(ge=0.0, le=1.0)


class LessonOutput(BaseModel):
    lesson: str

    @field_validator("lesson")
    @classmethod
    def _length_bound(cls, value: str) -> str:
        return value[:500]
