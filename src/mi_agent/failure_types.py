from __future__ import annotations

import hashlib
import re

UNCLASSIFIED_FAILURE = "unclassified_failure"
_FAILURE_KEY_MAX_LEN = 64


def normalize_failure_type(raw: str) -> str:
    text = raw.strip().lower()
    if text == "":
        return UNCLASSIFIED_FAILURE

    simplified = re.sub(r"[^a-z0-9]+", " ", text)
    tokens = set(simplified.split())
    if not tokens:
        return UNCLASSIFIED_FAILURE

    if _looks_like_db_file_missing(text, tokens):
        return "db_file_missing"
    if _looks_like_db_corrupt(text, tokens):
        return "db_corrupt"
    if _looks_like_disk_full(text, tokens):
        return "disk_full"
    if _looks_like_service_down(text, tokens):
        return "service_down"

    slug = "_".join(simplified.split())
    if slug == "":
        return UNCLASSIFIED_FAILURE
    if len(slug) <= _FAILURE_KEY_MAX_LEN:
        return slug
    digest = hashlib.sha1(slug.encode("utf-8")).hexdigest()[:10]
    prefix_len = _FAILURE_KEY_MAX_LEN - len(digest) - 1
    prefix = slug[:prefix_len].rstrip("_")
    if prefix == "":
        return digest
    return f"{prefix}_{digest}"


def _looks_like_db_file_missing(text: str, tokens: set[str]) -> bool:
    direct_signatures = (
        "unable to open database file",
        "database file is missing",
        "missing_db",
        "missing db",
        "database_initialization_storage_missing",
        "database_initialization_storage_misconfiguration",
        "database_initialization_migration_missing",
        "database initialization migration not performed",
    )
    if any(signature in text for signature in direct_signatures):
        return True

    has_db_context = bool({"db", "database", "sqlite"} & tokens)
    has_missing_signal = bool(
        {
            "missing",
            "unavailable",
            "misconfiguration",
            "misconfigured",
            "not",
            "storage",
            "migration",
            "initialization",
            "schema",
        }
        & tokens
    )
    return has_db_context and has_missing_signal


def _looks_like_db_corrupt(text: str, tokens: set[str]) -> bool:
    if "database disk image is malformed" in text:
        return True
    if "corrupt" in tokens and ({"db", "database", "sqlite"} & tokens) != set():
        return True
    return "malformed" in tokens and "database" in tokens


def _looks_like_disk_full(text: str, tokens: set[str]) -> bool:
    if "no space left on device" in text:
        return True
    return "disk" in tokens and "full" in tokens


def _looks_like_service_down(text: str, tokens: set[str]) -> bool:
    if "connection refused" in text or "service unavailable" in text:
        return True
    return (
        "service" in tokens
        and ("down" in tokens or "unavailable" in tokens or "stopped" in tokens)
    )

