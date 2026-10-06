from __future__ import annotations

import pytest

from mi_agent.events.bus import EventBus
from mi_agent.events.models import Event


@pytest.mark.asyncio()
async def test_event_bus_subscriber_failure_isolated() -> None:
    bus = EventBus()

    async def bad_subscriber(event: Event) -> None:
        del event
        raise RuntimeError("boom")

    called = 0

    async def good_subscriber(event: Event) -> None:
        nonlocal called
        called += 1
        del event

    bus.subscribe(bad_subscriber)
    bus.subscribe(good_subscriber)

    await bus.publish(Event(event_type="x"))

    assert called == 1
    assert any(ev.event_type == "event_bus.subscriber_error" for ev in bus.events)
