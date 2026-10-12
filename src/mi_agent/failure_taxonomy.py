from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import TypedDict

from pydantic import BaseModel, Field, TypeAdapter

from mi_agent.failure_types import normalize_failure_type
from mi_agent.state.store import JSONFileStateStore


class ReviewItem(TypedDict):
    candidate: str
    nearest: str
    similarity: float


class TaxonomyState(BaseModel):
    known_types: list[str] = Field(default_factory=list)
    review_queue: list[ReviewItem] = Field(default_factory=list)


@dataclass(slots=True)
class FailureTaxonomy:
    path: Path
    merge_threshold: float = 0.9
    review_threshold: float = 0.7
    _adapter: TypeAdapter[TaxonomyState] = field(init=False)
    _store: JSONFileStateStore[TaxonomyState] = field(init=False)
    _state: TaxonomyState = field(init=False)

    def __post_init__(self) -> None:
        self._adapter = TypeAdapter(TaxonomyState)
        self._store = JSONFileStateStore[TaxonomyState](
            self.path,
            default_factory=TaxonomyState,
            decode=self._decode,
            encode=lambda state: state.model_dump(mode="json"),
        )
        self._state = self._store.load()

    def resolve(self, raw_failure_type: str) -> str:
        candidate = normalize_failure_type(raw_failure_type)
        if candidate not in self._state.known_types:
            nearest, similarity = self._nearest(candidate)
            if nearest is not None and similarity >= self.merge_threshold:
                return nearest
            if nearest is not None and similarity >= self.review_threshold:
                self._enqueue_review(candidate, nearest, similarity)
            self._state.known_types.append(candidate)
            self._persist()
        return candidate

    def review_queue(self) -> list[ReviewItem]:
        return [item.copy() for item in self._state.review_queue]

    def _nearest(self, candidate: str) -> tuple[str | None, float]:
        if not self._state.known_types:
            return None, 0.0
        nearest = max(
            self._state.known_types,
            key=lambda item: SequenceMatcher(None, candidate, item).ratio(),
        )
        similarity = SequenceMatcher(None, candidate, nearest).ratio()
        return nearest, similarity

    def _enqueue_review(self, candidate: str, nearest: str, similarity: float) -> None:
        existing = [
            item
            for item in self._state.review_queue
            if item["candidate"] == candidate and item["nearest"] == nearest
        ]
        if existing:
            return
        self._state.review_queue.append(
            {
                "candidate": candidate,
                "nearest": nearest,
                "similarity": round(similarity, 4),
            }
        )

    def _persist(self) -> None:
        self._store.save(self._state)

    def _decode(self, raw: object) -> TaxonomyState:
        return self._adapter.validate_python(raw)
