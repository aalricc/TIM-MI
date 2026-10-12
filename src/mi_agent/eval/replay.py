from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class ReplayLLMClient:
    replay_file: Path
    _entries: list[dict[str, object]] = field(init=False, repr=False)
    _index: int = field(init=False, default=0, repr=False)

    def __post_init__(self) -> None:
        content = json.loads(self.replay_file.read_text(encoding="utf-8"))
        if not isinstance(content, list):
            raise ValueError("Replay file must contain a JSON list")
        self._entries = content
        self._index = 0

    def complete(self, prompt: str) -> str:
        del prompt
        if self._index >= len(self._entries):
            raise RuntimeError("Replay exhausted before evaluation finished")
        item = self._entries[self._index]
        self._index += 1
        if not isinstance(item, dict):
            raise ValueError("Replay entries must be objects")
        response = item.get("response")
        if not isinstance(response, str):
            raise ValueError("Replay entry missing string 'response'")
        return response
