from __future__ import annotations

from typing import Any


class PostgresStateStoreStub:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    @property
    def dsn(self) -> str:
        return self._dsn

    def save(self, key: str, payload: dict[str, Any], *, schema_version: int = 1) -> None:
        del key, payload, schema_version
        raise NotImplementedError("Postgres adapter is a contract-only stub in Phase 3")

    def load(self, key: str) -> dict[str, Any] | None:
        del key
        raise NotImplementedError("Postgres adapter is a contract-only stub in Phase 3")

