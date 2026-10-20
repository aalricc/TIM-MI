from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mi_agent.rag_query import (
    apply_query_prefix_and_cap,
    build_repair_query,
    build_symptom_query,
    normalize_query_text,
    prepare_retrieval_query,
)


@dataclass(frozen=True, slots=True)
class RAGEvalCase:
    query: str
    expected_sources: tuple[str, ...]
    required: bool = True
    note: str | None = None
    query_raw: str | None = None
    query_normalized: str | None = None
    diagnose_queries: tuple[str, ...] = tuple()


@dataclass(frozen=True, slots=True)
class RAGEvalHit:
    source: str
    score: float | None
    section: str | None
    chunk_index: int | None


@dataclass(frozen=True, slots=True)
class RAGEvalResult:
    case: RAGEvalCase
    top_hits: tuple[RAGEvalHit, ...]
    passed: bool


QUERY_VARIANTS: tuple[str, ...] = ("current", "sqlite", "sqlite-recovery")


def load_eval_cases(path: Path) -> list[RAGEvalCase]:
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("RAG eval cases file must be a JSON array")

    cases: list[RAGEvalCase] = []
    for row in payload:
        if not isinstance(row, dict):
            raise ValueError("RAG eval case must be an object")
        query = row.get("query")
        query_raw = row.get("query_raw")
        query_normalized = row.get("query_normalized")
        expected_sources = row.get("expected_sources")
        required = bool(row.get("required", True))
        note = row.get("note")
        diagnose_queries = row.get("diagnose_queries", [])

        if query is None and isinstance(query_normalized, str):
            query = query_normalized
        if query is None and isinstance(query_raw, str):
            query = query_raw
        if not isinstance(query, str) or query.strip() == "":
            raise ValueError(
                "RAG eval case must define query or query_raw/query_normalized as non-empty strings"
            )
        if query_raw is not None and (not isinstance(query_raw, str) or query_raw.strip() == ""):
            raise ValueError("RAG eval case query_raw must be a non-empty string when provided")
        if query_normalized is not None and (
            not isinstance(query_normalized, str) or query_normalized.strip() == ""
        ):
            raise ValueError(
                "RAG eval case query_normalized must be a non-empty string when provided"
            )
        if not isinstance(expected_sources, list) or not expected_sources:
            raise ValueError("RAG eval case expected_sources must be a non-empty array")
        if not all(isinstance(item, str) and item.strip() for item in expected_sources):
            raise ValueError("RAG eval case expected_sources entries must be non-empty strings")
        if note is not None and not isinstance(note, str):
            raise ValueError("RAG eval case note must be a string when provided")
        if not isinstance(diagnose_queries, list):
            raise ValueError("RAG eval case diagnose_queries must be an array when provided")
        if not all(isinstance(item, str) and item.strip() for item in diagnose_queries):
            raise ValueError(
                "RAG eval case diagnose_queries entries must be non-empty strings"
            )

        cases.append(
            RAGEvalCase(
                query=query,
                expected_sources=tuple(expected_sources),
                required=required,
                note=note,
                query_raw=query_raw,
                query_normalized=query_normalized,
                diagnose_queries=tuple(diagnose_queries),
            )
        )
    return cases


def evaluate_cases(
    *,
    cases: list[RAGEvalCase],
    search_fn: Callable[[str, int], list[dict[str, Any]]],
    top_k: int = 3,
) -> list[RAGEvalResult]:
    results: list[RAGEvalResult] = []
    safe_top_k = max(1, top_k)

    for case in cases:
        rows = search_fn(case.query, safe_top_k)
        hits: list[RAGEvalHit] = []
        for row in rows[:safe_top_k]:
            metadata_raw = row.get("metadata")
            metadata: dict[str, Any]
            metadata = metadata_raw if isinstance(metadata_raw, dict) else {}
            section = metadata.get("section") if isinstance(metadata.get("section"), str) else None
            chunk_index = metadata.get("chunk_index")
            if not isinstance(chunk_index, int):
                chunk_index = None
            score = row.get("score")
            if not isinstance(score, float):
                score = None
            source = str(row.get("source") or "unknown")
            hits.append(
                RAGEvalHit(
                    source=source,
                    score=score,
                    section=section,
                    chunk_index=chunk_index,
                )
            )

        expected = {value.strip() for value in case.expected_sources}
        passed = any(hit.source in expected for hit in hits)
        results.append(RAGEvalResult(case=case, top_hits=tuple(hits), passed=passed))
    return results


def summarize_results(results: list[RAGEvalResult]) -> dict[str, int]:
    required_total = sum(1 for result in results if result.case.required)
    required_passed = sum(1 for result in results if result.case.required and result.passed)
    optional_total = sum(1 for result in results if not result.case.required)
    optional_passed = sum(1 for result in results if not result.case.required and result.passed)
    return {
        "required_total": required_total,
        "required_passed": required_passed,
        "optional_total": optional_total,
        "optional_passed": optional_passed,
    }


def build_case_query_for_cap(
    case: RAGEvalCase,
    *,
    max_chars: int,
    query_prefix: str | None = "",
    rewrite_enabled: bool = True,
) -> str:
    normalized = build_case_normalized_query(case)
    return prepare_retrieval_query(
        query=normalized,
        prefix=query_prefix,
        max_chars=max_chars,
        rewrite_enabled=rewrite_enabled,
    )


def build_case_normalized_query(case: RAGEvalCase) -> str:
    raw = case.query_raw
    if not isinstance(raw, str) or raw.strip() == "":
        baseline = case.query_normalized if isinstance(case.query_normalized, str) else case.query
        return normalize_query_text(baseline, max_chars=10_000)

    mode, payload = _parse_raw_case_query(raw)
    if mode == "repair":
        return build_repair_query(
            diagnostics=payload.get("diagnostics", {}),
            failure_type=payload.get("failure_type"),
            hypotheses=payload.get("hypotheses", []),
            last_tool_error=payload.get("last_tool_error"),
            max_chars=None,
        )

    diagnostics = payload.get("diagnostics", {})
    return build_symptom_query(diagnostics, max_chars=None)


def build_split_subqueries(
    normalized_query: str,
    *,
    query_prefix: str | None,
    max_chars: int,
    min_words: int = 3,
    max_segments: int = 3,
) -> tuple[str, ...]:
    raw_segments = [segment.strip() for segment in normalized_query.split(" | ")]
    candidate_segments = [
        segment
        for segment in raw_segments
        if segment and len(segment.split()) >= max(1, min_words)
    ]
    if len(candidate_segments) < 2:
        return tuple()

    selected = candidate_segments[: max(1, max_segments)]
    return tuple(
        apply_query_prefix_and_cap(
            query=segment,
            prefix=query_prefix,
            max_chars=max_chars,
        )
        for segment in selected
    )


def reciprocal_rank_fuse(
    result_lists: list[list[dict[str, Any]]],
    *,
    top_k: int,
    rrf_k: int = 60,
) -> list[dict[str, Any]]:
    safe_top_k = max(1, top_k)
    safe_rrf_k = max(1, rrf_k)
    scores: dict[tuple[str, int | None], float] = {}
    rows: dict[tuple[str, int | None], dict[str, Any]] = {}

    for entries in result_lists:
        for rank, row in enumerate(entries[:safe_top_k], start=1):
            metadata_raw = row.get("metadata")
            metadata: dict[str, Any]
            metadata = metadata_raw if isinstance(metadata_raw, dict) else {}
            chunk_index = metadata.get("chunk_index")
            if isinstance(chunk_index, float) and chunk_index.is_integer():
                chunk_index = int(chunk_index)
            if not isinstance(chunk_index, int):
                chunk_index = None

            source = str(row.get("source") or "unknown")
            key = (source, chunk_index)

            scores[key] = scores.get(key, 0.0) + (1.0 / (safe_rrf_k + rank))
            if key not in rows:
                rows[key] = {
                    "source": source,
                    "score": row.get("score"),
                    "metadata": {
                        **metadata,
                        "chunk_index": chunk_index,
                    },
                }

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    fused: list[dict[str, Any]] = []
    for key, fused_score in ranked[:safe_top_k]:
        row = dict(rows[key])
        row["score"] = fused_score
        fused.append(row)
    return fused


def rank_expected_sources(
    rows: list[dict[str, Any]],
    expected_sources: tuple[str, ...],
    *,
    depth: int,
) -> dict[str, tuple[int | None, float | None]]:
    safe_depth = max(1, depth)
    rankings: dict[str, tuple[int | None, float | None]] = {
        source: (None, None) for source in expected_sources
    }

    for rank, row in enumerate(rows[:safe_depth], start=1):
        source = str(row.get("source") or "")
        if source not in rankings:
            continue
        current_rank, _ = rankings[source]
        if current_rank is not None:
            continue
        score = row.get("score")
        rankings[source] = (rank, score if isinstance(score, float) else None)
    return rankings


def count_expected_sources_found(
    rows: list[dict[str, Any]],
    expected_sources: tuple[str, ...],
    *,
    limit: int,
) -> int:
    safe_limit = max(1, limit)
    expected_set = {source.strip() for source in expected_sources}
    found = {
        str(row.get("source") or "")
        for row in rows[:safe_limit]
        if str(row.get("source") or "") in expected_set
    }
    return len(found)


def apply_query_variant(query: str, variant: str) -> str:
    if variant == "current":
        return query
    if variant == "sqlite":
        return f"sqlite | {query}" if query else "sqlite"
    if variant == "sqlite-recovery":
        return f"sqlite recovery | {query}" if query else "sqlite recovery"
    raise ValueError(f"Unknown query variant: {variant}")


def parse_query_variants(value: str | None) -> list[str]:
    raw = (value or "all").strip().lower()
    if raw in {"", "all"}:
        return list(QUERY_VARIANTS)

    selected: list[str] = []
    for token in raw.split(","):
        token = token.strip()
        if token == "":
            continue
        if token not in QUERY_VARIANTS:
            raise ValueError(f"Unsupported query variant: {token}")
        if token not in selected:
            selected.append(token)
    return selected or list(QUERY_VARIANTS)


def _parse_raw_case_query(raw: str) -> tuple[str, dict[str, Any]]:
    parts = [part.strip() for part in raw.split("|") if part.strip()]
    lowered = [part.lower() for part in parts]

    if "sqlite repair guidance" in lowered or "dynamic recovery" in lowered:
        failure_type: str | None = None
        hypotheses: list[str] = []
        last_tool_error: str | None = None
        for part in parts:
            low = part.lower()
            if low in {"sqlite repair guidance", "dynamic recovery"}:
                continue
            if low.startswith("hypothesis="):
                hypotheses.append(part.split("=", 1)[1].strip())
                continue
            if low.startswith("latest_tool_error="):
                last_tool_error = part.split("=", 1)[1].strip()
                continue
            if failure_type is None:
                failure_type = part
            else:
                hypotheses.append(part)
        return (
            "repair",
            {
                "diagnostics": {},
                "failure_type": failure_type,
                "hypotheses": hypotheses,
                "last_tool_error": last_tool_error,
            },
        )

    health_error: str | None = None
    integrity_row: str | None = None
    log_line: str | None = None
    for part in parts:
        low = part.lower()
        if low in {"sqlite recovery", "symptom grounding"}:
            continue
        if low.startswith("log_tail="):
            log_line = part.split("=", 1)[1].strip()
            continue
        if health_error is None:
            health_error = part
        elif integrity_row is None:
            integrity_row = part
        elif log_line is None:
            log_line = part

    diagnostics: dict[str, Any] = {}
    if health_error:
        diagnostics["health_deep"] = {"error": health_error}
    if integrity_row:
        diagnostics["db_integrity"] = {"rows": [integrity_row]}
    if log_line:
        diagnostics["log_tail"] = [log_line]
    return "symptom", {"diagnostics": diagnostics}
