from __future__ import annotations

from pathlib import Path
from shutil import copytree, rmtree


class SandboxResetter:
    def __init__(self, *, baseline_root: Path, sandbox_root: Path) -> None:
        self._baseline_root = baseline_root
        self._sandbox_root = sandbox_root

    def reset(self) -> None:
        if self._sandbox_root.exists():
            rmtree(self._sandbox_root)
        self._sandbox_root.parent.mkdir(parents=True, exist_ok=True)
        copytree(self._baseline_root, self._sandbox_root)

