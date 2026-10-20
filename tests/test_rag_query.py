from __future__ import annotations

from mi_agent.rag_query import (
    RAG_QUERY_MAX_CHARS,
    apply_conditional_prefix,
    build_repair_query,
    build_symptom_query,
    cap_chunks_per_source,
    clean_query_for_retrieval,
    enrich_with_error_codes,
    is_recovery_query,
    normalize_query_parts,
    normalize_query_text,
)


def test_build_symptom_query_orders_error_text_before_log_and_normalizes() -> None:
    diagnostics = {
        "health_deep": {
            "error": (
                "SQLITE_BUSY database is locked at 2026-10-20T10:14:22Z "
                "request_id=8d35b8aa-f57f-4f9d-b3a9-190d38af74d3 /var/lib/app/data/app.db"
            ),
        },
        "db_integrity": {"rows": ["no such table: widgets"]},
        "log_tail": [
            (
                "trace=cbf9cf8f-83d2-4f6f-a228-664f7e3f2f3b "
                "app failed path=/srv/prod/db/main.sqlite ***"
            )
        ],
    }

    query = build_symptom_query(diagnostics)
    assert query.startswith("SQLITE_BUSY database is locked")
    assert "no such table: widgets" in query
    assert "trace=" not in query
    assert "2026-10-20T10:14:22Z" not in query
    assert "/var/lib/app/data/app.db" not in query
    assert "***" not in query
    assert "integrity check error" in query
    assert " while path " not in f" {query} "
    assert " path " not in f" {query} "


def test_build_repair_query_keeps_result_codes_and_strips_long_digits() -> None:
    diagnostics = {"log_tail": ["code 2067 and run 1234567890 in /tmp/app.db"]}
    query = build_repair_query(
        diagnostics=diagnostics,
        failure_type="SQLITE_CORRUPT",
        hypotheses=["no such table widgets", "result code 2067"],
        last_tool_error=(
            "run_sql failed 2026-10-20T10:15:11Z "
            "request_id=3f90aafc-4338-4642-8579-b63b574f80bc"
        ),
    )

    assert "SQLITE_CORRUPT" in query
    assert "no such table widgets" in query
    assert "2067" in query
    assert "1234567890" not in query
    assert "request_id" not in query
    assert "2026-10-20T10:15:11Z" not in query
    assert " at " not in f" {query} "


def test_normalize_query_text_drops_dangling_labels() -> None:
    query = normalize_query_text(
        "integrity row says *** while path /srv/prod/db/main.sqlite at 2026-10-20T10:15:11Z "
        "log_tail=request_id=3f90aafc-4338-4642-8579-b63b574f80bc",
        max_chars=300,
    )
    assert "integrity check error" in query
    assert "while path" not in query
    assert " path " not in f" {query} "
    assert " at " not in f" {query} "
    assert "log_tail=" not in query
    assert "request_id=" not in query


def test_normalize_query_parts_caps_to_max_chars() -> None:
    long_fragment = "x" * (RAG_QUERY_MAX_CHARS * 2)
    query = normalize_query_parts([long_fragment])
    assert len(query) == RAG_QUERY_MAX_CHARS


def test_normalize_query_text_truncates_on_word_boundary() -> None:
    query = normalize_query_text(
        "alpha bravo charlie delta echo foxtrot golf",
        max_chars=22,
    )
    assert query == "alpha bravo charlie"


def test_is_recovery_query_matches_keywords_case_insensitive() -> None:
    assert is_recovery_query("integrity_check failed") is True
    assert is_recovery_query("Disk image is malformed") is True
    assert is_recovery_query("random timeout") is False


def test_clean_query_for_retrieval_removes_leadins_identifiers_and_tokens() -> None:
    query = (
        "log says no such table: widgets hypothesis=btree page corrupted "
        "latest_tool_error=run_sql failed /srv/prod/db/main.sqlite "
        "3f90aafc-4338-4642-8579-b63b574f80bc 0xdeadbeefcafefeed"
    )
    cleaned = clean_query_for_retrieval(query)
    assert "log says" not in cleaned.lower()
    assert "hypothesis=" not in cleaned.lower()
    assert "latest_tool_error=" not in cleaned.lower()
    assert "widgets" not in cleaned.lower()
    assert "no such table" in cleaned.lower()
    assert "/srv/prod/db/main.sqlite" not in cleaned
    assert "3f90aafc" not in cleaned
    assert "0xdeadbeef" not in cleaned.lower()


def test_clean_query_for_retrieval_never_returns_empty() -> None:
    query = " /srv/prod/db/main.sqlite "
    cleaned = clean_query_for_retrieval(query)
    assert cleaned.strip() != ""


def test_enrich_with_error_codes_limits_to_three_and_dedupes() -> None:
    query = (
        "database is locked and readonly database with constraint failed "
        "and no such table users"
    )
    enriched = enrich_with_error_codes(query)
    codes = [token for token in enriched.replace("|", " ").split() if token.startswith("SQLITE_")]
    assert len(codes) == 3
    assert len(set(codes)) == len(codes)


def test_apply_conditional_prefix_only_for_recovery_queries() -> None:
    prefixed = apply_conditional_prefix(
        query="database disk image is malformed",
        prefix="sqlite",
        max_chars=300,
    )
    assert prefixed.startswith("sqlite |")

    not_prefixed = apply_conditional_prefix(
        query="service timeout while polling",
        prefix="sqlite",
        max_chars=300,
    )
    assert not_prefixed == "service timeout while polling"

    empty_prefix = apply_conditional_prefix(
        query="database disk image is malformed",
        prefix="   ",
        max_chars=300,
    )
    assert empty_prefix == "database disk image is malformed"


def test_cap_chunks_per_source_preserves_order_and_limit() -> None:
    hits = [
        {"source": "A", "metadata": {"chunk_index": 1}},
        {"source": "A", "metadata": {"chunk_index": 2}},
        {"source": "A", "metadata": {"chunk_index": 3}},
        {"source": "B", "metadata": {"chunk_index": 1}},
        {"source": "B", "metadata": {"chunk_index": 2}},
    ]
    capped = cap_chunks_per_source(hits, max_per_source=2, k=4)
    assert [row["metadata"]["chunk_index"] for row in capped if row["source"] == "A"] == [1, 2]
    assert [row["metadata"]["chunk_index"] for row in capped if row["source"] == "B"] == [1, 2]
    assert len(capped) == 4

