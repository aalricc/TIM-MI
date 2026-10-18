from __future__ import annotations

from pathlib import Path

from mi_agent.config import MISettings
from mi_agent.errors import SecurityGateError


class PathGuard:
    def __init__(self, settings: MISettings) -> None:
        self._settings = settings

    def resolve_in_sandbox(self, raw_path: str) -> Path:
        resolved = (self._settings.sandbox_root / raw_path).resolve(strict=False)
        sandbox_root = self._settings.sandbox_root.resolve(strict=False)
        if not self._is_within(resolved, sandbox_root):
            raise SecurityGateError(f"path escapes sandbox: {raw_path}")
        return resolved

    def resolve_in_app_root(self, raw_path: str) -> Path:
        resolved = (self._settings.app_root / raw_path).resolve(strict=False)
        app_root = self._settings.app_root.resolve(strict=False)
        if not self._is_within(resolved, app_root):
            raise SecurityGateError(f"path escapes app root: {raw_path}")
        return resolved

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False
