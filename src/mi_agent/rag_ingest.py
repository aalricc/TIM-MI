from __future__ import annotations

import hashlib
import importlib
import re
from pathlib import Path
from typing import Any

_PRINT_HEADER_RE = re.compile(
    r"^\d{1,2}/\d{1,2}/\d{2,4},\s+\d{1,2}:\d{2}\s*(?:AM|PM)\b.*$",
    re.IGNORECASE,
)
_PRINT_PAGE_RE = re.compile(r"^Page\s+\d+\s+of\s+\d+\s*$", re.IGNORECASE)
_URL_PAGE_RE = re.compile(r"^https?://\S+\s+\d+/\d+\s*$", re.IGNORECASE)
_NAV_BOILERPLATE_RE = re.compile(
    r"small\.\s*fast\.\s*reliable\.\s*choose any three\.|\bhome\b.*\bmenu\b.*\babout\b",
    re.IGNORECASE,
)
_SOURCE_BROWSER_CHROME_RE = re.compile(
    r"^Annotate\s+Artifact\s+Blame\s+Check-?ins\s+Using\s+Doc\s+Download\s+Hex\s+Line\s+Numbers$",
    re.IGNORECASE,
)
_TAGLINE_RE = re.compile(r"small\W+fast\W+reliable\W+choose any three\W*", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")
_ENDS_WITH_PERIOD_RE = re.compile(r"\.$")
_REAL_SECTION_HEADING_RE = re.compile(r"^\d+(?:\.\d+)*\.\s+[A-Z][^\n]*$")
_BARE_NUMERIC_TOKEN_RE = re.compile(r"^\d+(?:\.\d+)*\s")
_BARE_ALPHA_TOKEN_RE = re.compile(r"^[A-Za-z]\.\s")
_ENTRY_HEADING_RE = re.compile(r"^\((\d+)\)\s+(SQLITE_[A-Z0-9_]+)\b")
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_PAREN_DATE_RE = re.compile(r"\(\d{4}-\d{2}-\d{2}\)")
_HEX_BYTE_RUN_RE = re.compile(r"(?:\b[0-9a-fA-F]{2}\b(?:\s+|$)){4,}")
_NAV_TOKENS = {
    "home",
    "menu",
    "about",
    "purchase",
    "search",
    "docs",
    "documentation",
    "download",
    "contact",
}
_NAV_EXACT_LINES = {
    "purchase search",
    "home menu about",
}
_CHROME_EXACT_LINES = {
    "column contents",
    "contents column",
}

_ARTIFACT_FIXES: tuple[tuple[str, str], ...] = (
    ("andincluding", "and including"),
    ("andor", "and or"),
)

UPSERT_BATCH_SIZE = 90


def extract_pdf_pages(path: Path) -> list[tuple[int, str]]:
    try:
        pypdf_module = importlib.import_module("pypdf")
    except ModuleNotFoundError as exc:
        raise RuntimeError("pypdf is required for PDF ingestion") from exc
    PdfReader = pypdf_module.PdfReader

    reader = PdfReader(path)
    pages: list[tuple[int, str]] = []
    for index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        pages.append((index, text))
    return pages


def clean_extracted_text(text: str) -> tuple[str, list[tuple[str, str]]]:
    cleaned_lines: list[str] = []
    samples: list[tuple[str, str]] = []

    for raw_line in text.splitlines():
        after, sample = _clean_line(raw_line)
        if after is None or after == "":
            continue
        if sample is not None and len(samples) < 6:
            samples.append(sample)
        cleaned_lines.append(after)

    return "\n".join(cleaned_lines), samples


def chunk_document(
    *,
    source_file: str,
    pages: list[tuple[int, str]],
    target_chars: int = 1800,
    overlap_chars: int = 240,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    if target_chars < 400:
        raise ValueError("target_chars must be >= 400")
    if overlap_chars < 0:
        raise ValueError("overlap_chars must be >= 0")

    section_title = "document"
    section_page = 1
    section_paragraphs: list[tuple[int, str]] = []
    sections: list[tuple[str, int, list[tuple[int, str]]]] = []
    cleanup_samples: list[tuple[str, str]] = []

    def flush_section() -> None:
        if section_paragraphs:
            sections.append((section_title, section_page, list(section_paragraphs)))
            section_paragraphs.clear()

    for page_number, page_text in pages:
        cleaned_text, samples = clean_extracted_text(page_text)
        for sample in samples:
            if len(cleanup_samples) < 8:
                cleanup_samples.append(sample)

        if cleaned_text == "":
            continue

        for line in cleaned_text.splitlines():
            if _looks_like_heading(line):
                flush_section()
                section_title = line
                section_page = page_number
                continue
            section_paragraphs.append((page_number, line))

    flush_section()

    records: list[dict[str, Any]] = []
    seen_text_hashes: set[str] = set()

    for title, default_page, paragraphs in sections:
        merged_text = " ".join(
            paragraph.strip() for _, paragraph in paragraphs if paragraph.strip()
        )
        sentence_units = [
            unit.strip() for unit in _SENTENCE_SPLIT_RE.split(merged_text) if unit.strip()
        ]
        if not sentence_units and merged_text:
            sentence_units = [merged_text]

        chunk_parts: list[str] = []
        chunk_chars = 0
        for unit in sentence_units:
            proposed_chars = chunk_chars + (1 if chunk_parts else 0) + len(unit)
            if chunk_parts and proposed_chars > target_chars:
                text = " ".join(chunk_parts).strip()
                text = _strip_boilerplate_text(text)
                if text:
                    text_hash = _text_hash(text)
                    if text_hash not in seen_text_hashes:
                        seen_text_hashes.add(text_hash)
                        records.append(
                            {
                                "source_file": source_file,
                                "text": text,
                                "section": title,
                                "chunk_index": len(records),
                                "page": default_page,
                            }
                        )
                chunk_parts = []
                chunk_chars = 0

            if len(unit) > target_chars:
                if chunk_parts:
                    chunk_parts.append(unit)
                    chunk_chars = len(" ".join(chunk_parts))
                else:
                    chunk_parts = [unit]
                    chunk_chars = len(unit)
            else:
                chunk_parts.append(unit)
                chunk_chars = len(" ".join(chunk_parts))

        if chunk_parts:
            text = " ".join(chunk_parts).strip()
            text = _strip_boilerplate_text(text)
            if text:
                text_hash = _text_hash(text)
                if text_hash not in seen_text_hashes:
                    seen_text_hashes.add(text_hash)
                    records.append(
                        {
                            "source_file": source_file,
                            "text": text,
                            "section": title,
                            "chunk_index": len(records),
                            "page": default_page,
                        }
                    )

    for index, record in enumerate(records):
        record["chunk_index"] = index
    return records, cleanup_samples


def chunk_text_document(
    *,
    source_file: str,
    text: str,
    target_chars: int = 1800,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    if target_chars < 400:
        raise ValueError("target_chars must be >= 400")

    records: list[dict[str, Any]] = []
    seen_text_hashes: set[str] = set()
    samples: list[tuple[str, str]] = []
    section_title = "document"
    chunk_parts: list[str] = []

    def flush_chunk() -> None:
        if not chunk_parts:
            return
        content = " ".join(chunk_parts).strip()
        content = _strip_boilerplate_text(content)
        if not content:
            chunk_parts.clear()
            return

        digest = _text_hash(content)
        if digest in seen_text_hashes:
            chunk_parts.clear()
            return
        seen_text_hashes.add(digest)
        records.append(
            {
                "source_file": source_file,
                "text": content,
                "section": section_title,
                "chunk_index": len(records),
            }
        )
        chunk_parts.clear()

    for raw_line in text.splitlines():
        cleaned_line, sample = _clean_line(raw_line)
        if sample is not None and len(samples) < 8:
            samples.append(sample)

        if cleaned_line is None:
            continue
        if cleaned_line == "":
            flush_chunk()
            continue

        md_heading = _MD_HEADING_RE.match(cleaned_line)
        if md_heading:
            flush_chunk()
            section_title = md_heading.group(1).strip() or "document"
            continue

        entry_match = _ENTRY_HEADING_RE.match(cleaned_line)
        if entry_match:
            flush_chunk()
            section_title = f"({entry_match.group(1)}) {entry_match.group(2)}"
            chunk_parts.append(cleaned_line)
            continue

        if not chunk_parts and _looks_like_heading(cleaned_line):
            section_title = cleaned_line
            continue

        proposed_chars = len(" ".join(chunk_parts)) + (1 if chunk_parts else 0) + len(cleaned_line)
        if chunk_parts and proposed_chars > target_chars:
            flush_chunk()
        chunk_parts.append(cleaned_line)

    flush_chunk()

    for index, record in enumerate(records):
        record["chunk_index"] = index
    return records, samples


def dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for record in records:
        section = str(record.get("section") or "")
        text = str(record.get("text") or "")
        digest = hashlib.sha1(
            f"{section}|{_normalize(text)}".encode(), usedforsecurity=False
        ).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        deduped.append(record)

    return deduped


def ensure_write_allowed(*, namespace: str, allow_live: bool, dry_run: bool) -> None:
    if dry_run:
        return
    if namespace == "sqlite-docs" and not allow_live:
        raise ValueError(
            "Refusing to write to live namespace 'sqlite-docs' without --allow-live"
        )


def delete_namespace_records(*, index: Any, namespace: str) -> bool:
    try:
        index.delete(delete_all=True, namespace=namespace)
    except Exception as exc:  # pragma: no cover - type depends on pinecone runtime
        if _is_namespace_not_found(exc):
            return False
        raise
    return True


def upsert_records(*, index: Any, namespace: str, records: list[dict[str, Any]]) -> int:
    batch_size = UPSERT_BATCH_SIZE
    upserted = 0
    for start in range(0, len(records), batch_size):
        batch = [to_upsert_record(item) for item in records[start : start + batch_size]]
        index.upsert_records(namespace, batch)
        upserted += len(batch)
    return upserted


def to_upsert_record(record: dict[str, Any]) -> dict[str, Any]:
    source_file = str(record["source_file"])
    chunk_index = int(record["chunk_index"])
    section = str(record.get("section") or "document")
    text_body = str(record["text"])
    text_with_prefix = f"{source_file} — {section}\n{text_body}" if section else text_body
    payload: dict[str, Any] = {
        "_id": f"{source_file}:{chunk_index}",
        "source_file": source_file,
        "text": text_with_prefix,
        "section": section,
        "chunk_index": chunk_index,
    }
    page = record.get("page")
    if isinstance(page, int):
        payload["page"] = page
    return payload


def _text_hash(text: str) -> str:
    return hashlib.sha1(_normalize(text).encode("utf-8"), usedforsecurity=False).hexdigest()


def _normalize(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value.strip().lower())


def _strip_boilerplate_text(text: str) -> str:
    stripped = _TAGLINE_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", stripped).strip()


def _clean_line(raw_line: str) -> tuple[str | None, tuple[str, str] | None]:
    before = raw_line.strip()
    if before == "":
        return "", None

    collapsed = _WHITESPACE_RE.sub(" ", before).strip()
    if _PRINT_HEADER_RE.match(collapsed):
        return None, None
    if _PRINT_PAGE_RE.match(collapsed):
        return None, None
    if _URL_PAGE_RE.match(collapsed):
        return None, None
    if _NAV_BOILERPLATE_RE.search(collapsed):
        return None, None
    if _SOURCE_BROWSER_CHROME_RE.match(collapsed):
        return None, None
    if _is_chrome_label(collapsed):
        return None, None
    if _is_navigation_line(collapsed):
        return None, None
    if collapsed.lower() == "small. fast. reliable. choose any three.":
        return None, None

    after = collapsed
    for needle, replacement in _ARTIFACT_FIXES:
        if needle in after:
            after = after.replace(needle, replacement)
    after = after.replace("Small. Fast. Reliable. Choose any three.", "").strip()
    after = _WHITESPACE_RE.sub(" ", after).strip()
    if after == "":
        return None, None

    sample = (before, after) if after != before else None
    return after, sample


def _looks_like_heading(line: str) -> bool:
    text = line.strip()
    if text == "":
        return False
    if len(text) > 60:
        return False
    if _is_navigation_line(text):
        return False
    if _is_chrome_label(text):
        return False
    if "://" in text:
        return False
    if _PAREN_DATE_RE.search(text):
        return False
    if _HEX_BYTE_RUN_RE.search(text):
        return False
    if _ENDS_WITH_PERIOD_RE.search(text):
        return False
    if _REAL_SECTION_HEADING_RE.match(text):
        return True
    if _starts_with_bare_token(text):
        return False
    if "✔" in text:
        return False

    words = [token for token in re.split(r"\s+", text) if token]
    if not words:
        return False
    if len(words) > 12:
        return False

    alpha_words = [token for token in words if any(char.isalpha() for char in token)]
    if not alpha_words:
        return False

    if text.isupper() and len(text) >= 6:
        return True

    title_like = 0
    for token in alpha_words:
        plain = re.sub(r"^[^A-Za-z]+|[^A-Za-z]+$", "", token)
        if plain == "":
            continue
        if plain[0].isupper() and not plain.isupper():
            title_like += 1
    if title_like == 0:
        return False

    ratio = title_like / len(alpha_words)
    return ratio >= 0.6


def _is_navigation_line(line: str) -> bool:
    lowered = _WHITESPACE_RE.sub(" ", line.strip().lower())
    if lowered in _NAV_EXACT_LINES:
        return True
    tokens = [token for token in lowered.split(" ") if token]
    if not tokens:
        return False
    return len(tokens) <= 6 and all(token in _NAV_TOKENS for token in tokens)


def _is_chrome_label(line: str) -> bool:
    lowered = _WHITESPACE_RE.sub(" ", line.strip().lower())
    if lowered in _CHROME_EXACT_LINES:
        return True
    return lowered.startswith("column ") and "contents" in lowered


def _starts_with_bare_token(text: str) -> bool:
    if _REAL_SECTION_HEADING_RE.match(text):
        return False
    if _BARE_NUMERIC_TOKEN_RE.match(text):
        return True
    if _BARE_ALPHA_TOKEN_RE.match(text):
        return True
    return bool(_HEX_BYTE_RUN_RE.match(text))


def _is_namespace_not_found(exc: Exception) -> bool:
    status = getattr(exc, "status", None)
    if status == 404:
        return True
    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    if response_status == 404:
        return True

    message = str(exc).lower()
    return "404" in message and "namespace" in message and "not found" in message
