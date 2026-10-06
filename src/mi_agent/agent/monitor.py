from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from mi_agent.events.bus import EventBus
from mi_agent.events.models import Event


class MonitorLoop:
    def __init__(
        self,
        *,
        interval_seconds: float,
        ping_fn: Callable[[], Awaitable[bool]],
        on_failure: Callable[[], Awaitable[None]],
        event_bus: EventBus,
    ) -> None:
        self._interval_seconds = interval_seconds
        self._ping_fn = ping_fn
        self._on_failure = on_failure
        self._event_bus = event_bus
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="mi-monitor-loop")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is None:
            return
        await self._task
        self._task = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            cycle_started = asyncio.get_running_loop().time()
            try:
                ok = await self._ping_fn()
                await self._event_bus.publish(Event(event_type="monitor.ping", payload={"ok": ok}))
                if not ok:
                    await self._on_failure()
            except Exception as exc:
                await self._event_bus.publish(
                    Event(event_type="monitor.error", payload={"error": str(exc)})
                )
            elapsed = asyncio.get_running_loop().time() - cycle_started
            sleep_for = max(0.0, self._interval_seconds - elapsed)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_for)
            except TimeoutError:
                continue
