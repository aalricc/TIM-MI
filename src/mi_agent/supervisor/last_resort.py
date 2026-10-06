from __future__ import annotations

from pathlib import Path
from shutil import copy2
from typing import Protocol

import httpx


class LastResortError(Exception):
    """Raised when backup restore fails."""


class LastResortClient(Protocol):
    async def restore_latest(self) -> str: ...


class LocalLastResortClient:
    def __init__(self, backups_root: Path, db_path: Path) -> None:
        self._backups_root = backups_root
        self._db_path = db_path

    async def restore_latest(self) -> str:
        self._backups_root.mkdir(parents=True, exist_ok=True)
        candidates = sorted(self._backups_root.glob("*.db"), key=lambda p: p.stat().st_mtime)
        if not candidates:
            raise LastResortError("No backups available")
        source = candidates[-1]
        if source.stat().st_size == 0:
            raise LastResortError("Backup appears corrupt (empty file)")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        copy2(source, self._db_path)
        return str(source)


class HTTPLastResortClient:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    async def restore_latest(self) -> str:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(f"{self._base_url}/restore")
        if response.status_code >= 400:
            raise LastResortError(response.text)
        payload = response.json()
        backup_path = payload.get("backup_path")
        if not isinstance(backup_path, str):
            raise LastResortError("Missing backup path in last-resort response")
        return backup_path
