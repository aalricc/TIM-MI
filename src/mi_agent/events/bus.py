from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from mi_agent.events.models import Event

type Subscriber = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self, event_log_path: Path | None = None) -> None:
        self._subscribers: list[Subscriber] = []
        self._events: list[Event] = []
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._event_log_path = event_log_path

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    def subscribe(self, callback: Subscriber) -> None:
        self._subscribers.append(callback)

    async def publish(self, event: Event) -> None:
        self._events.append(event)
        self._append_event_log(event)
        await self._queue.put(event)
        for subscriber in list(self._subscribers):
            try:
                await subscriber(event)
            except Exception as exc:  # pragma: no cover - explicitly handled by emitting event
                failure = Event(
                    event_type="event_bus.subscriber_error",
                    payload={"error": str(exc), "source_event": event.event_type},
                )
                self._events.append(failure)
                self._append_event_log(failure)
                await self._queue.put(failure)

    async def next_event(self) -> Event:
        return await self._queue.get()

    def _append_event_log(self, event: Event) -> None:
        if self._event_log_path is None:
            return
        self._event_log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = event.model_dump(mode="json")
        with self._event_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
