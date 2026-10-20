from __future__ import annotations

import re
from typing import Any

_ISO_TS_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"
)
_UUID_LIKE_RE = re.compile(r"\b[0-9a-fA-F]{8,}(?:-[0-9a-fA-F]{4,}){1,}\b")
_REQUEST_ID_RE = re.compile(
    r"\b(?:request[_-]?id|trace[_-]?id|trace|correlation[_-]?id)=[^\s|]*",
    re.IGNORECASE,
)
_UNIX_ABS_PATH_RE = re.compile(r"(?:(?<=\s)|(?<==)|(?<=:)|^)/(?:[\w.+@-]+/)+[\w.+@-]+")
_WIN_ABS_PATH_RE = re.compile(r"\b[A-Za-z]:\\\\[^\s|]+")
_STAR_RUN_RE = re.compile(r"\*{3,}")
_HEX_TOKEN_RE = re.compile(r"\b(?:0x)?[0-9a-fA-F]{12,}\b")
_LONG_DIGIT_RE = re.compile(r"\b\d{5,}\b")
_DANGLING_LABEL_RE = re.compile(
    r"\b(?:log_tail|request[_-]?id)\s*=\s*|\bwhile\s+path\b|\b(at|path)\b(?=\s*(?:\||$))",
    re.IGNORECASE,
)
_TRAILING_LABEL_RE = re.compile(r"\b(?:at|path)\b$", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")
_NO_SUCH_IDENTIFIER_RE = re.compile(
    r"\b(no\s+such\s+(?:table|column|index|view|function|collation)\s*:?)\s+[^\s|]+",
    re.IGNORECASE,
)
_FILLER_LEADINS_RE = re.compile(
    r"\b(?:log\s+says|integrity\s+row\s+says|error\s+says)\b\s*:?,?",
    re.IGNORECASE,
)
_HYPOTHESIS_LABEL_RE = re.compile(r"\b(?:hypothesis|latest_tool_error)\s*=\s*", re.IGNORECASE)

_RECOVERY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bcorrupt(?:ion|ed)?\b", re.IGNORECASE),
    re.compile(r"\bmalformed\b", re.IGNORECASE),
    re.compile(r"\brecover(?:y|ed|ing)?\b", re.IGNORECASE),
    re.compile(r"\bintegrity(?:\s+check|_check)\b", re.IGNORECASE),
    re.compile(r"\bnot\s+a\s+database\b", re.IGNORECASE),
    re.compile(r"\bdisk\s+image\b", re.IGNORECASE),
    re.compile(r"\bb-?tree\s+page\b", re.IGNORECASE),
    re.compile(r"\bchecksum\b", re.IGNORECASE),
)

_ERROR_CODE_PHRASES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bdatabase\s+is\s+locked\b", re.IGNORECASE), "SQLITE_BUSY"),
    (re.compile(r"\bdatabase\s+table\s+is\s+locked\b", re.IGNORECASE), "SQLITE_LOCKED"),
    (re.compile(r"\bdisk\s+image\s+is\s+malformed\b", re.IGNORECASE), "SQLITE_CORRUPT"),
    (re.compile(r"\bno\s+such\s+(?:table|column)\b", re.IGNORECASE), "SQLITE_ERROR"),
    (re.compile(r"\bunable\s+to\s+open\s+database\s+file\b", re.IGNORECASE), "SQLITE_CANTOPEN"),
    (re.compile(r"\breadonly\s+database\b", re.IGNORECASE), "SQLITE_READONLY"),
    (re.compile(r"\bdatabase\s+or\s+disk\s+is\s+full\b", re.IGNORECASE), "SQLITE_FULL"),
    (re.compile(r"\bconstraint\s+failed\b", re.IGNORECASE), "SQLITE_CONSTRAINT"),
    (re.compile(r"\bfile\s+is\s+not\s+a\s+database\b", re.IGNORECASE), "SQLITE_NOTADB"),
    (re.compile(r"\bdisk\s+I/O\s+error\b", re.IGNORECASE), "SQLITE_IOERR"),
)

RAG_QUERY_MAX_CHARS = 300


def build_symptom_query(
    diagnostics: dict[str, Any],
    *,
    max_chars: int | None = RAG_QUERY_MAX_CHARS,
) -> str:
    health_error = _health_error(diagnostics)
    integrity_row = _integrity_row(diagnostics)
    log_line = _log_line(diagnostics)
    return normalize_query_parts([health_error, integrity_row, log_line], max_chars=max_chars)


def build_repair_query(
    *,
    diagnostics: dict[str, Any],
    failure_type: str | None,
    hypotheses: list[str],
    last_tool_error: str | None,
    max_chars: int | None = RAG_QUERY_MAX_CHARS,
) -> str:
    parts: list[str | None] = [
        _health_error(diagnostics),
        _integrity_row(diagnostics),
        failure_type,
        *hypotheses[:2],
        last_tool_error,
        _log_line(diagnostics),
    ]
    return normalize_query_parts(parts, max_chars=max_chars)


def normalize_query_parts(
    parts: list[str | None],
    *,
    max_chars: int | None = RAG_QUERY_MAX_CHARS,
) -> str:
    cleaned: list[str] = []
    for value in parts:
        if not isinstance(value, str):
            continue
        normalized = _normalize_fragment(value)
        if normalized:
            cleaned.append(normalized)

    merged = " | ".join(cleaned)
    merged = _WHITESPACE_RE.sub(" ", merged).strip(" |")
    if max_chars is None:
        return merged
    return _truncate_at_word_boundary(merged, max_chars=max_chars)


def normalize_query_text(value: str, *, max_chars: int = RAG_QUERY_MAX_CHARS) -> str:
    return _truncate_at_word_boundary(_normalize_fragment(value), max_chars=max_chars)


def apply_query_prefix_and_cap(*, query: str, prefix: str | None, max_chars: int) -> str:
    prefix_value = (prefix or "").strip()
    normalized_query = query.strip()
    if prefix_value:
        query_lower = normalized_query.lower()
        prefix_lower = prefix_value.lower()
        if not query_lower.startswith(prefix_lower):
            normalized_query = (
                f"{prefix_value} | {normalized_query}" if normalized_query else prefix_value
            )
    return _truncate_at_word_boundary(normalized_query, max_chars=max_chars)


def is_recovery_query(query: str) -> bool:
    value = query.strip()
    if value == "":
        return False
    return any(pattern.search(value) for pattern in _RECOVERY_PATTERNS)


def clean_query_for_retrieval(query: str) -> str:
    original = _WHITESPACE_RE.sub(" ", query).strip()
    if original == "":
        return query

    cleaned = original
    cleaned = _FILLER_LEADINS_RE.sub(" ", cleaned)
    cleaned = _HYPOTHESIS_LABEL_RE.sub(" ", cleaned)
    cleaned = _NO_SUCH_IDENTIFIER_RE.sub(r"\1", cleaned)
    cleaned = _UNIX_ABS_PATH_RE.sub(" ", cleaned)
    cleaned = _WIN_ABS_PATH_RE.sub(" ", cleaned)
    cleaned = _UUID_RE.sub(" ", cleaned)
    cleaned = _UUID_LIKE_RE.sub(" ", cleaned)
    cleaned = _HEX_TOKEN_RE.sub(" ", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip(" |")
    return cleaned or original


def enrich_with_error_codes(query: str) -> str:
    normalized = _WHITESPACE_RE.sub(" ", query).strip()
    if normalized == "":
        return query

    existing_codes = {
        token.upper()
        for token in normalized.replace("|", " ").split()
        if token.upper().startswith("SQLITE_")
    }
    additions: list[str] = []
    for pattern, code in _ERROR_CODE_PHRASES:
        if len(additions) >= 3:
            break
        if code in existing_codes or code in additions:
            continue
        if pattern.search(normalized):
            additions.append(code)

    if not additions:
        return normalized
    return f"{normalized} | {' | '.join(additions)}"


def apply_conditional_prefix(*, query: str, prefix: str | None, max_chars: int) -> str:
    normalized_query = _WHITESPACE_RE.sub(" ", query).strip()
    if is_recovery_query(normalized_query):
        return apply_query_prefix_and_cap(
            query=normalized_query,
            prefix=prefix,
            max_chars=max_chars,
        )
    return _truncate_at_word_boundary(normalized_query, max_chars=max_chars)


def prepare_retrieval_query(
    *,
    query: str,
    prefix: str | None,
    max_chars: int,
    rewrite_enabled: bool,
) -> str:
    if not rewrite_enabled:
        return apply_query_prefix_and_cap(query=query, prefix=prefix, max_chars=max_chars)

    cleaned = clean_query_for_retrieval(query)
    enriched = enrich_with_error_codes(cleaned)
    return apply_conditional_prefix(query=enriched, prefix=prefix, max_chars=max_chars)


def cap_chunks_per_source(
    hits: list[Any],
    *,
    max_per_source: int = 2,
    k: int,
) -> list[Any]:
    safe_k = max(1, k)
    safe_max_per_source = max(1, max_per_source)
    counts: dict[str, int] = {}
    capped: list[Any] = []

    for hit in hits:
        if isinstance(hit, dict):
            source = str(hit.get("source") or "unknown")
        else:
            source = str(getattr(hit, "source", "unknown") or "unknown")

        if counts.get(source, 0) >= safe_max_per_source:
            continue

        capped.append(hit)
        counts[source] = counts.get(source, 0) + 1
        if len(capped) >= safe_k:
            break

    return capped


def _normalize_fragment(value: str) -> str:
    normalized = value
    normalized = _STAR_RUN_RE.sub(" integrity check error ", normalized)
    normalized = _ISO_TS_RE.sub(" ", normalized)
    normalized = _REQUEST_ID_RE.sub(" ", normalized)
    normalized = _UUID_RE.sub(" ", normalized)
    normalized = _UNIX_ABS_PATH_RE.sub(" ", normalized)
    normalized = _WIN_ABS_PATH_RE.sub(" ", normalized)
    normalized = _LONG_DIGIT_RE.sub(" ", normalized)
    normalized = _DANGLING_LABEL_RE.sub(" ", normalized)
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip(" |")
    normalized = _TRAILING_LABEL_RE.sub("", normalized).strip(" |")
    return normalized


def _truncate_at_word_boundary(value: str, *, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    truncated = value[: max(0, max_chars)].rstrip()
    split_at = truncated.rfind(" ")
    if split_at > 0:
        truncated = truncated[:split_at]
    return truncated.rstrip(" |")


def _health_error(diagnostics: dict[str, Any]) -> str | None:
    health = diagnostics.get("health_deep")
    if isinstance(health, dict):
        error = health.get("error")
        if isinstance(error, str) and error.strip() != "":
            return error
    return None


def _integrity_row(diagnostics: dict[str, Any]) -> str | None:
    integrity = diagnostics.get("db_integrity")
    if isinstance(integrity, dict):
        rows = integrity.get("rows")
        if isinstance(rows, list) and rows:
            first = rows[0]
            if isinstance(first, str) and first.strip() != "":
                return first
    return None


def _log_line(diagnostics: dict[str, Any]) -> str | None:
    log_tail = diagnostics.get("log_tail")
    if isinstance(log_tail, list) and log_tail:
        value = log_tail[-1]
        if isinstance(value, str) and value.strip() != "":
            return value
    return None

