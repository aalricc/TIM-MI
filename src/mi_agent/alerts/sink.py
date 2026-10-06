from __future__ import annotations

from typing import Protocol

import httpx


class AlertSink(Protocol):
    async def send(self, title: str, message: str) -> None: ...


class ConsoleAlertSink:
    async def send(self, title: str, message: str) -> None:
        print(f"[ALERT] {title}: {message}")


class WebhookAlertSink:
    def __init__(self, webhook_url: str) -> None:
        self._webhook_url = webhook_url

    async def send(self, title: str, message: str) -> None:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(self._webhook_url, json={"title": title, "message": message})
