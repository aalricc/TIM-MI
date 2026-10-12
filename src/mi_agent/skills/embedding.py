from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


class Embedder(Protocol):
    def embed(self, text: str) -> list[float]: ...


@dataclass(frozen=True, slots=True)
class DeterministicEmbedder:
    dimension: int = 32

    def embed(self, text: str) -> list[float]:
        clean = re.sub(r"\s+", " ", text.strip().lower())
        if clean == "":
            return [0.0] * self.dimension

        vector = [0.0] * self.dimension
        for token in clean.split(" "):
            index = int(hashlib.sha1(token.encode("utf-8")).hexdigest(), 16) % self.dimension
            vector[index] += 1.0

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or len(left) == 0:
        return 0.0
    dot = sum(left[index] * right[index] for index in range(len(left)))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def fingerprint_text(fingerprint: dict[str, str]) -> str:
    parts = [f"{key}={value}" for key, value in sorted(fingerprint.items())]
    return "\n".join(parts)

