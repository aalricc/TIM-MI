from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator


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


class SetPermissionsParams(BaseModel):
    path: str
    mode: Literal[0o600, 0o640, 0o660, 0o700, 0o750]


class RestartServiceParams(BaseModel):
    service_name: str = "target-app"


class ActionChoice(BaseModel):
    action_id: str
    parameters: dict[str, object] = Field(default_factory=dict)


class CandidatePlan(BaseModel):
    name: str
    rationale: str
    steps: list[ActionChoice] = Field(min_length=1, max_length=10)


class DiagnosisOutput(BaseModel):
    failure_type: str
    hypotheses: list[str] = Field(min_length=1, max_length=5)
    evidence: list[str] = Field(min_length=1, max_length=10)


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


def safe_rel_path(path: str) -> Path:
    clean = path.strip()
    if clean == "":
        raise ValueError("Path cannot be empty")
    return Path(clean)
