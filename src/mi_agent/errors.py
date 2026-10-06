from __future__ import annotations


class MIError(Exception):
    """Base MI exception."""


class ToolError(MIError):
    """Base tool exception."""


class PrimitiveNotAllowedError(ToolError):
    """Raised when primitive is outside allowlist."""


class ToolParameterValidationError(ToolError):
    """Raised on invalid tool parameters."""


class PathScopeError(ToolError):
    """Raised when path escapes the allowed sandbox scope."""


class BackupAccessDeniedError(PathScopeError):
    """Raised when backups path is accessed by the agent."""


class IncidentLockError(MIError):
    """Raised when supervisor lock cannot be acquired."""


class LLMUnavailableError(MIError):
    """Raised when LLM provider is unavailable."""


class LLMOutputValidationError(MIError):
    """Raised for malformed LLM structured output."""


class CorruptStoreError(MIError):
    """Raised for corrupted policy / skill storage."""
