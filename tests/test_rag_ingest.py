from __future__ import annotations

from mi_agent.rag_ingest import (
    UPSERT_BATCH_SIZE,
    chunk_document,
    chunk_text_document,
    dedupe_records,
    delete_namespace_records,
    ensure_write_allowed,
    to_upsert_record,
    upsert_records,
)


def test_chunk_document_cleans_headers_and_artifacts() -> None:
    pages = [
        (
            1,
            "10/16/26, 1:31 PM SQLite\n"
            "Small. Fast. Reliable. Choose any three. Home Menu About\n"
            "Purchase Search\n"
            "Write-Ahead Logging\n"
            "This section explains andincluding checkpoints.\n"
            "https://www.sqlite.org/rescode.html 12/20\n"
            "Page 1 of 3\n",
        )
    ]

    records, samples = chunk_document(source_file="Write-Ahead Logging", pages=pages)
    assert records
    assert records[0]["source_file"] == "Write-Ahead Logging"
    assert "Small. Fast. Reliable" not in records[0]["text"]
    assert "Purchase Search" not in records[0]["text"]
    assert "https://www.sqlite.org/rescode.html 12/20" not in records[0]["text"]
    assert "and including" in records[0]["text"]
    assert samples


def test_dedupe_and_ids_are_stable() -> None:
    records = [
        {
            "source_file": "DocA",
            "text": "same text",
            "section": "A",
            "chunk_index": 0,
            "page": 1,
        },
        {
            "source_file": "DocA",
            "text": "same text",
            "section": "A",
            "chunk_index": 1,
            "page": 1,
        },
    ]
    deduped = dedupe_records(records)
    assert len(deduped) == 1
    payload = to_upsert_record(deduped[0])
    assert payload["_id"] == "DocA:0"
    assert payload["source_file"] == "DocA"
    assert payload["text"].startswith("DocA — A\n")
    assert payload["chunk_index"] == 0


def test_sentence_line_is_not_treated_as_heading() -> None:
    pages = [
        (
            1,
            "Database File Format\n"
            "The default configuration of SQLite assumes the underlying "
            "filesystem supports long filenames.\n"
            "The rollback journal may be used for recovery.\n",
        )
    ]

    records, _ = chunk_document(source_file="Database File Format", pages=pages)
    assert records
    first = records[0]
    assert first["section"] == "Database File Format"
    assert "The default configuration of SQLite assumes" in first["text"]


def test_live_namespace_write_requires_allow_flag() -> None:
    try:
        ensure_write_allowed(namespace="sqlite-docs", allow_live=False, dry_run=False)
    except ValueError as exc:
        assert "--allow-live" in str(exc)
    else:
        raise AssertionError("Expected live namespace protection")


def test_chunk_text_document_groups_sqlite_entries() -> None:
    text = (
        "# sqlite_error_message_glossary\n\n"
        "(5) SQLITE_BUSY Error message text: \"database is locked\"\n"
        "The database file is locked by another process.\n\n"
        "(11) SQLITE_CORRUPT Error message text: \"database disk image is malformed\"\n"
        "The file is malformed and needs recovery.\n"
    )

    records, _ = chunk_text_document(source_file="sqlite_error_message_glossary", text=text)
    assert len(records) == 2
    assert records[0]["section"] == "(5) SQLITE_BUSY"
    assert "database is locked" in records[0]["text"]
    assert records[1]["section"] == "(11) SQLITE_CORRUPT"


def test_chunk_document_no_word_tail_overlap() -> None:
    pages = [
        (
            1,
            "Result and Error Codes\n"
            + "Paragraph one sentence. " * 120
            + "\n"
            + "Paragraph two starts cleanly and should not begin with a trailing suffix. " * 120
            + "\n",
        )
    ]
    records, _ = chunk_document(source_file="Result and Error Codes", pages=pages, target_chars=700)
    assert len(records) >= 2
    assert not records[1]["text"].startswith("suffix")


def test_chunk_document_strips_source_browser_chrome_and_column_contents() -> None:
    pages = [
        (
            1,
            "SQLite_ sqlite3recover.h\n"
            "Annotate Artifact Blame Check-ins Using Doc Download Hex Line Numbers\n"
            "Column Contents\n"
            "sqlite3_recover_init() initializes the recover object.\n",
        )
    ]

    records, _ = chunk_document(source_file="SQLite_ sqlite3recover.h", pages=pages)
    assert records
    assert records[0]["section"] != "Column Contents"
    assert "Annotate Artifact Blame" not in records[0]["text"]
    assert "Column Contents" not in records[0]["text"]


def test_heading_rejection_keeps_previous_valid_section() -> None:
    pages = [
        (
            1,
            "1.1. Hot Journals\n"
            "3.7.0 (2010-07-21), the use of NPTL is assumed\n"
            "4062 entries are on the first 32768-byte unit\n"
            "74 65 20 66 6f 72 6d 61 74 20 33 00\n"
            "c. It is not required that one be remote\n"
            "This paragraph should remain under the valid section.\n",
        )
    ]

    records, _ = chunk_document(source_file="Database File Format", pages=pages)
    assert records
    assert records[0]["section"] == "1.1. Hot Journals"
    assert "3.7.0 (2010-07-21), the use of NPTL is assumed" in records[0]["text"]
    assert "4062 entries are on the first 32768-byte unit" in records[0]["text"]
    assert "74 65 20 66 6f 72 6d 61 74 20 33 00" in records[0]["text"]
    assert "c. It is not required that one be remote" in records[0]["text"]


def test_heading_longer_than_sixty_chars_is_not_heading() -> None:
    long_line = (
        "This Heading Is Intentionally Extremely Long To Exceed The Sixty Character Limit"
    )
    assert len(long_line) > 60
    pages = [
        (
            1,
            "1. Intro\n"
            f"{long_line}\n"
            "Body text remains in the intro section\n",
        )
    ]

    records, _ = chunk_document(source_file="Result and Error Codes", pages=pages)
    assert records
    assert records[0]["section"] == "1. Intro"
    assert long_line in records[0]["text"]


def test_upsert_records_caps_batches_to_constant() -> None:
    calls: list[int] = []

    class _Index:
        def upsert_records(self, namespace: str, batch: list[dict[str, object]]) -> None:
            calls.append(len(batch))

    records = [
        {
            "source_file": "DocA",
            "text": f"text-{idx}",
            "section": "A",
            "chunk_index": idx,
        }
        for idx in range(UPSERT_BATCH_SIZE * 2 + 7)
    ]
    upserted = upsert_records(index=_Index(), namespace="sqlite-docs-v2", records=records)
    assert upserted == len(records)
    assert all(size <= UPSERT_BATCH_SIZE for size in calls)
    assert calls == [UPSERT_BATCH_SIZE, UPSERT_BATCH_SIZE, 7]


def test_delete_namespace_records_ignores_404_not_found() -> None:
    class _NotFoundError(RuntimeError):
        status = 404

    class _Index:
        def delete(self, *, delete_all: bool, namespace: str) -> None:
            raise _NotFoundError("404 Namespace not found")

    deleted = delete_namespace_records(index=_Index(), namespace="sqlite-docs-v2")
    assert deleted is False
