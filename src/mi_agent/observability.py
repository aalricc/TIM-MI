from __future__ import annotations

import copy
import re
from typing import Any

_REDACTED = "[REDACTED]"
_SECRET_KEY_MARKERS = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "authorization",
    "bearer",
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)bearer\s+[a-z0-9._\-]+"),
    re.compile(r"\b(sk|pcsk|pc)[-_][a-z0-9\-_]{10,}\b", re.IGNORECASE),
)


def sanitize_payload(
    payload: Any,
    *,
    max_depth: int = 6,
    max_items: int = 60,
    max_string_length: int = 2000,
) -> Any:
    redacted = _redact(copy.deepcopy(payload))
    return _truncate(
        redacted,
        depth=0,
        max_depth=max_depth,
        max_items=max_items,
        max_string_length=max_string_length,
    )


def _redact(value: Any, *, key_hint: str | None = None) -> Any:
    if key_hint is not None and _looks_secret_key(key_hint):
        return _REDACTED

    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            output[text_key] = _redact(item, key_hint=text_key)
        return output

    if isinstance(value, list):
        return [_redact(item, key_hint=key_hint) for item in value]

    if isinstance(value, tuple):
        return tuple(_redact(item, key_hint=key_hint) for item in value)

    if isinstance(value, str):
        return _redact_string(value)

    return value


def _truncate(
    value: Any,
    *,
    depth: int,
    max_depth: int,
    max_items: int,
    max_string_length: int,
) -> Any:
    if depth >= max_depth:
        return "<truncated:depth>"

    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for idx, (key, item) in enumerate(value.items()):
            if idx >= max_items:
                output["__truncated__"] = f"{len(value) - max_items} keys"
                break
            output[str(key)] = _truncate(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                max_string_length=max_string_length,
            )
        return output

    if isinstance(value, list):
        output_list: list[Any] = []
        for idx, item in enumerate(value):
            if idx >= max_items:
                output_list.append(f"<truncated:{len(value) - max_items} items>")
                break
            output_list.append(
                _truncate(
                    item,
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_items=max_items,
                    max_string_length=max_string_length,
                )
            )
        return output_list

    if isinstance(value, tuple):
        return tuple(
            _truncate(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                max_string_length=max_string_length,
            )
            for item in value[:max_items]
        )

    if isinstance(value, str):
        if len(value) <= max_string_length:
            return value
        return f"{value[:max_string_length]}...<truncated:{len(value) - max_string_length} chars>"

    return value


def _looks_secret_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return any(marker in lowered for marker in _SECRET_KEY_MARKERS)


def _redact_string(value: str) -> str:
    redacted = value
    for pattern in _SECRET_VALUE_PATTERNS:
        redacted = pattern.sub(_REDACTED, redacted)
    return redacted
