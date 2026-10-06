from __future__ import annotations

import asyncio

import pytest

from mi_agent.agent.monitor import MonitorLoop
from mi_agent.events.bus import EventBus


@pytest.mark.asyncio()
async def test_monitor_fixed_interval_and_start_stop() -> None:
    bus = EventBus()
    calls = 0

    async def ping() -> bool:
        nonlocal calls
        calls += 1
        return True

    failures = 0

    async def on_failure() -> None:
        nonlocal failures
        failures += 1

    loop = MonitorLoop(interval_seconds=0.02, ping_fn=ping, on_failure=on_failure, event_bus=bus)
    await loop.start()
    await asyncio.sleep(0.09)
    await loop.stop()

    assert calls >= 3
    assert failures == 0


@pytest.mark.asyncio()
async def test_monitor_no_overlap_and_transient_error_survival() -> None:
    bus = EventBus()
    concurrent = 0
    max_concurrent = 0
    calls = 0

    async def ping() -> bool:
        nonlocal concurrent, max_concurrent, calls
        calls += 1
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        try:
            await asyncio.sleep(0.015)
            if calls == 2:
                raise RuntimeError("transient")
            return calls % 2 == 0
        finally:
            concurrent -= 1

    failure_calls = 0

    async def on_failure() -> None:
        nonlocal failure_calls
        failure_calls += 1

    loop = MonitorLoop(interval_seconds=0.01, ping_fn=ping, on_failure=on_failure, event_bus=bus)
    await loop.start()
    await asyncio.sleep(0.08)
    await loop.stop()

    assert max_concurrent == 1
    assert calls >= 3
    assert failure_calls >= 1
    assert any(event.event_type == "monitor.error" for event in bus.events)
