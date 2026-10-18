from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mi_agent.observability import sanitize_payload


@dataclass(slots=True)
class Event:
    ts: float
    event_type: str
    payload: dict[str, Any]


class EventLog:
    def __init__(
        self,
        event_log_path: Path,
        *,
        max_events: int = 4000,
        max_payload_string_length: int = 1200,
    ) -> None:
        self._event_log_path = event_log_path
        self._events: list[Event] = []
        self._max_events = max_events
        self._max_payload_string_length = max_payload_string_length

    @property
    def events(self) -> list[Event]:
        return list(self._events)

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        safe_payload = sanitize_payload(
            payload,
            max_string_length=self._max_payload_string_length,
        )
        if not isinstance(safe_payload, dict):
            safe_payload = {"value": safe_payload}
        event = Event(ts=time.time(), event_type=event_type, payload=safe_payload)
        self._events.append(event)
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events :]
        self._event_log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": event.ts,
            "event_type": event.event_type,
            "payload": event.payload,
        }
        with self._event_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def tail(self, limit: int = 200) -> list[dict[str, Any]]:
        return [
            {"ts": e.ts, "event_type": e.event_type, "payload": e.payload}
            for e in self._events[-limit:]
        ]
