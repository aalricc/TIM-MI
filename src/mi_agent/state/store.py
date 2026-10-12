from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol, TypeVar

TState = TypeVar("TState")


class StateStore(Protocol[TState]):
    def load(self) -> TState: ...

    def save(self, state: TState) -> None: ...


class JSONFileStateStore[TState]:
    def __init__(
        self,
        path: Path,
        *,
        default_factory: Callable[[], TState],
        decode: Callable[[object], TState],
        encode: Callable[[TState], object],
    ) -> None:
        self._path = path
        self._default_factory = default_factory
        self._decode = decode
        self._encode = encode
        self._lock_path = path.with_suffix(f"{path.suffix}.lock")

    def load(self) -> TState:
        with _exclusive_file_lock(self._lock_path):
            if not self._path.exists():
                return self._default_factory()
            raw = self._path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        return self._decode(payload)

    def save(self, state: TState) -> None:
        payload = self._encode(state)
        self._path.parent.mkdir(parents=True, exist_ok=True)

        with _exclusive_file_lock(self._lock_path):
            with NamedTemporaryFile(
                mode="w",
                delete=False,
                dir=self._path.parent,
                encoding="utf-8",
            ) as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
                temp_path = Path(handle.name)
            os.replace(temp_path, self._path)
            _fsync_dir(self._path.parent)


@contextmanager
def _exclusive_file_lock(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
