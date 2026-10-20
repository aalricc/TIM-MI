from __future__ import annotations

from pathlib import Path
from typing import Any

from mi_agent.rag_eval import (
    apply_query_variant,
    build_case_normalized_query,
    build_case_query_for_cap,
    build_split_subqueries,
    count_expected_sources_found,
    evaluate_cases,
    load_eval_cases,
    parse_query_variants,
    rank_expected_sources,
    reciprocal_rank_fuse,
    summarize_results,
)


def test_load_eval_cases_file() -> None:
    path = Path("tests/data/rag_eval_cases.json")
    cases = load_eval_cases(path)
    assert len(cases) >= 8
    assert any(not case.required for case in cases)
    observational_pairs = [
        case for case in cases if not case.required and case.query_raw and case.query_normalized
    ]
    assert len(observational_pairs) >= 3
    required_case = next(case for case in cases if case.required)
    assert required_case.diagnose_queries == tuple()

    widgets_case = next(
        case for case in cases if "no such table widgets" in case.query.lower()
    )
    assert len(widgets_case.diagnose_queries) == 4


def test_evaluate_cases_checks_expected_source_in_top3() -> None:
    cases = load_eval_cases(Path("tests/data/rag_eval_cases.json"))

    def _search(query: str, k: int) -> list[dict[str, Any]]:
        del k
        if "database is locked" in query:
            return [
                {"source": "sqlite_error_message_glossary", "score": 0.94, "metadata": {}},
                {"source": "other", "score": 0.41, "metadata": {}},
                {"source": "other-2", "score": 0.33, "metadata": {}},
            ]
        return [
            {"source": "other", "score": 0.51, "metadata": {}},
            {"source": "other-2", "score": 0.44, "metadata": {}},
            {"source": "other-3", "score": 0.37, "metadata": {}},
        ]

    results = evaluate_cases(cases=cases, search_fn=_search, top_k=3)
    summary = summarize_results(results)
    assert summary["required_total"] >= 6
    assert summary["required_passed"] >= 1


def test_build_case_query_for_cap_uses_real_normalizer_from_raw_parts() -> None:
    cases = load_eval_cases(Path("tests/data/rag_eval_cases.json"))
    long_case = next(
        case
        for case in cases
        if case.query_raw and "symptom grounding" in case.query_raw
    )
    query = build_case_query_for_cap(long_case, max_chars=200)
    assert "symptom grounding" not in query
    assert "log_tail=" not in query
    assert "request_id" not in query


def test_query_variant_parsing_and_prefixing() -> None:
    assert parse_query_variants("all") == ["current", "sqlite", "sqlite-recovery"]
    assert parse_query_variants("sqlite") == ["sqlite"]
    assert apply_query_variant("database is locked", "current") == "database is locked"
    assert apply_query_variant("database is locked", "sqlite") == "sqlite | database is locked"
    assert (
        apply_query_variant("database is locked", "sqlite-recovery")
        == "sqlite recovery | database is locked"
    )


def test_build_case_query_for_cap_preserves_word_spaces_with_prefix() -> None:
    cases = load_eval_cases(Path("tests/data/rag_eval_cases.json"))
    case = next(
        item
        for item in cases
        if item.query_raw
        and "integrity row says ***" in item.query_raw
    )

    query = build_case_query_for_cap(
        case,
        max_chars=300,
        query_prefix="sqlite recovery",
    )

    assert "database main" in query
    assert "databasemain" not in query
    assert "no such table" in query
    assert "nosuch" not in query


def test_build_split_subqueries_drops_short_segments_and_caps_to_three() -> None:
    query = (
        "one two | alpha beta gamma | delta epsilon zeta eta | "
        "iota kappa lambda mu | nu xi omicron"
    )
    sub_queries = build_split_subqueries(
        query,
        query_prefix="sqlite recovery",
        max_chars=300,
    )
    assert len(sub_queries) == 3
    assert all(item.startswith("sqlite recovery | ") for item in sub_queries)
    assert "one two" not in " | ".join(sub_queries)


def test_build_split_subqueries_skips_when_less_than_two_segments() -> None:
    query = "one two | only valid segment with enough words"
    sub_queries = build_split_subqueries(
        query,
        query_prefix="sqlite recovery",
        max_chars=300,
    )
    assert sub_queries == tuple()


def test_reciprocal_rank_fuse_orders_and_dedupes_by_source_and_chunk_index() -> None:
    list_one = [
        {"source": "A", "score": 0.9, "metadata": {"chunk_index": 1}},
        {"source": "B", "score": 0.8, "metadata": {"chunk_index": 2}},
    ]
    list_two = [
        {"source": "B", "score": 0.95, "metadata": {"chunk_index": 2}},
        {"source": "C", "score": 0.85, "metadata": {"chunk_index": 3}},
    ]
    fused = reciprocal_rank_fuse([list_one, list_two], top_k=3, rrf_k=60)
    assert [row["source"] for row in fused] == ["B", "A", "C"]
    assert len([row for row in fused if row["source"] == "B"]) == 1


def test_build_case_normalized_query_matches_engine_normalizer_behavior() -> None:
    cases = load_eval_cases(Path("tests/data/rag_eval_cases.json"))
    case = next(
        item
        for item in cases
        if item.query_raw
        and "integrity row says ***" in item.query_raw
    )
    normalized = build_case_normalized_query(case)
    assert "integrity check error" in normalized


def test_rank_expected_sources_found_and_absent() -> None:
    rows = [
        {"source": "A", "score": 0.9, "metadata": {}},
        {"source": "B", "score": 0.8, "metadata": {}},
        {"source": "C", "score": 0.7, "metadata": {}},
    ]
    ranked = rank_expected_sources(rows, ("B", "Z"), depth=3)
    assert ranked["B"] == (2, 0.8)
    assert ranked["Z"] == (None, None)

    assert count_expected_sources_found(rows, ("B", "Z"), limit=2) == 1
