from __future__ import annotations

from typing import Protocol


class AlertSink(Protocol):
    async def send(self, title: str, message: str) -> None: ...


class ConsoleAlertSink:
    async def send(self, title: str, message: str) -> None:
        print(f"[ALERT] {title}: {message}")
