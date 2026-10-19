from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field


class RAGChunk(BaseModel):
    source: str
    text: str
    score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Diagnosis(BaseModel):
    failure_type: str
    hypotheses: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class DynamicToolSpec(BaseModel):
    name: str
    rationale: str
    operation: str
    params: dict[str, Any] = Field(default_factory=dict)


class HowProposal(BaseModel):
    primary: DynamicToolSpec
    alternatives: list[DynamicToolSpec] = Field(default_factory=list)


class MonitorStatus(BaseModel):
    healthy: bool
    status_code: int | None = None
    detail: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class VerificationResult(BaseModel):
    healthy: bool
    failure_type: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class AttemptFeedback(BaseModel):
    tool_name: str
    operation: str
    tool_error: str | None = None
    verifier_details: dict[str, Any] = Field(default_factory=dict)


@dataclass(slots=True)
class IncidentContext:
    incident_id: str
    attempts: int = 0
    last_tool_error: str | None = None
    state_fingerprint: str | None = None
    diagnosis: Diagnosis | None = None
    rag_symptom_chunks: list[RAGChunk] = field(default_factory=list)
    rag_repair_chunks: list[RAGChunk] = field(default_factory=list)
    prior_attempts: list[AttemptFeedback] = field(default_factory=list)
    latest_diagnostics: dict[str, Any] = field(default_factory=dict)
    failed_action_keys: set[str] = field(default_factory=set)
