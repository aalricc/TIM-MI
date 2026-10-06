from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from mi_agent.events.models import Event

type Subscriber = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []
        self._events: list[Event] = []
        self._queue: asyncio.Queue[Event] = asyncio.Queue()

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    def subscribe(self, callback: Subscriber) -> None:
        self._subscribers.append(callback)

    async def publish(self, event: Event) -> None:
        self._events.append(event)
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
                await self._queue.put(failure)

    async def next_event(self) -> Event:
        return await self._queue.get()
