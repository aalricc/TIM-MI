from __future__ import annotations


class MIError(Exception):
    """Base exception for MI agent failures."""


class StartupConfigError(MIError):
    """Raised when required runtime configuration is invalid."""


class LLMError(MIError):
    """Raised when an LLM call cannot be completed."""


class SecurityGateError(MIError):
    """Raised when a dynamic tool fails a security boundary check."""


class ToolExecutionError(MIError):
    """Raised when a tool execution path fails."""
