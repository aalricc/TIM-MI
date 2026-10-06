from __future__ import annotations

from pathlib import Path

from mi_agent.config import MISettings
from mi_agent.errors import BackupAccessDeniedError, PathScopeError


class PathGuard:
    def __init__(self, settings: MISettings) -> None:
        self._settings = settings

    def resolve_in_sandbox(self, relative_path: str) -> Path:
        base = self._settings.app_root
        candidate = (base / relative_path).resolve(strict=False)
        self._validate_path(candidate)
        return candidate

    def resolve_app_file(self, relative_path: str) -> Path:
        candidate = (self._settings.app_root / relative_path).resolve(strict=False)
        self._validate_path(candidate)
        app_root = self._settings.app_root.resolve(strict=False)
        if not self._is_within(candidate, app_root):
            raise PathScopeError(f"Path escapes app root: {relative_path}")
        return candidate

    def _validate_path(self, resolved_path: Path) -> None:
        sandbox_root = self._settings.sandbox_root.resolve(strict=False)
        backups_root = self._settings.backups_root.resolve(strict=False)
        if self._is_within(resolved_path, backups_root):
            raise BackupAccessDeniedError(f"Backup path access denied: {resolved_path}")
        if not self._is_within(resolved_path, sandbox_root):
            raise PathScopeError(f"Path outside sandbox: {resolved_path}")

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False
