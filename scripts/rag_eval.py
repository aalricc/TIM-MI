#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any


def _ensure_src_on_path() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    src_path = repo_root / "src"
    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))


_ensure_src_on_path()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate Pinecone RAG retrieval quality")
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path("tests/data/rag_eval_cases.json"),
        help="JSON file with eval queries and expected source_file values",
    )
    parser.add_argument(
        "--namespace",
        default="sqlite-docs",
        help="Pinecone namespace to evaluate",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=3,
        help="Top-k hits to fetch and score per query",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Optional path to save raw evaluation results as JSON",
    )
    parser.add_argument(
        "--query-max-chars",
        default="200,300,500",
        help="Comma-separated caps for normalized long-query comparison",
    )
    parser.add_argument(
        "--query-variant",
        default=os.getenv("MI_RAG_EVAL_QUERY_VARIANT", "all"),
        help="Query variant(s): current, sqlite, sqlite-recovery, all, or comma-separated",
    )
    parser.add_argument(
        "--split-queries",
        action="store_true",
        help="Run experimental split-query retrieval + RRF fusion (eval-only)",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Run observational-only diagnostics with compact variant output",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=10,
        help="Top-N retrieval depth used by --diagnose",
    )
    parser.add_argument(
        "--no-rewrite",
        action="store_true",
        help="Use legacy query behavior (always-prefix) for comparison",
    )
    return parser.parse_args(argv)


def main() -> int:
    from mi_agent.config import MISettings
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
    from mi_agent.sqlite_docs_tool import search_sqlite_docs

    args = _parse_args()
    api_key = os.getenv("PINECONE_API_KEY")
    index_host = os.getenv("PINECONE_INDEX_HOST")
    if not api_key or not index_host:
        print("PINECONE_API_KEY and PINECONE_INDEX_HOST are required", file=sys.stderr)
        return 2

    cases = load_eval_cases(args.cases)

    def _search(query: str, k: int) -> list[dict[str, Any]]:
        return search_sqlite_docs(
            query,
            k,
            api_key=api_key,
            index_host=index_host,
            namespace=args.namespace,
        )

    default_prefix = MISettings.model_fields["rag_query_prefix"].default
    default_max_chars = MISettings.model_fields["rag_query_max_chars"].default
    rag_query_prefix = str(os.getenv("MI_RAG_QUERY_PREFIX", default_prefix) or "")
    rag_query_max_chars = int(
        os.getenv("MI_RAG_QUERY_MAX_CHARS", str(default_max_chars))
    )
    rewrite_enabled = not args.no_rewrite

    safe_top_k = max(1, args.top_k)
    safe_depth = max(1, args.depth)

    def _chunk_index_value(row: dict[str, Any]) -> int | None:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        chunk_index = metadata.get("chunk_index")
        if isinstance(chunk_index, float) and chunk_index.is_integer():
            return int(chunk_index)
        if isinstance(chunk_index, int):
            return chunk_index
        return None

    def _top_three(rows: list[dict[str, Any]]) -> str:
        if not rows:
            return "none"
        items: list[str] = []
        for row in rows[:3]:
            source = str(row.get("source") or "unknown")
            score = row.get("score")
            score_text = f"{score:.4f}" if isinstance(score, float) else "n/a"
            chunk_index = _chunk_index_value(row)
            chunk_text = str(chunk_index) if chunk_index is not None else "?"
            items.append(f"{source}#{chunk_text}@{score_text}")
        return ", ".join(items)

    def _expected_summary(rows: list[dict[str, Any]], expected: tuple[str, ...]) -> str:
        ranked = rank_expected_sources(rows, expected, depth=safe_depth)
        parts: list[str] = []
        for source in expected:
            rank, score = ranked[source]
            if rank is None:
                parts.append(f"{source}:absent")
                continue
            score_text = f"{score:.4f}" if isinstance(score, float) else "n/a"
            parts.append(f"{source}:r{rank}@{score_text}")
        return "; ".join(parts)

    def _variant_line(
        *,
        label: str,
        query: str,
        rows: list[dict[str, Any]],
        expected: tuple[str, ...],
        retrieval_calls: int,
    ) -> tuple[int, int]:
        found_top_k = count_expected_sources_found(rows, expected, limit=safe_top_k)
        found_top_depth = count_expected_sources_found(rows, expected, limit=safe_depth)
        print(
            f"  [{label}] query={query!r} calls={retrieval_calls} "
            f"expected=({ _expected_summary(rows, expected) }) "
            f"top3=({ _top_three(rows) })"
        )
        return found_top_k, found_top_depth

    if args.diagnose:
        observational_cases = [case for case in cases if not case.required]
        print(f"namespace={args.namespace} mode=diagnose depth={safe_depth} top_k={safe_top_k}")

        table_rows: list[tuple[str, str, int, int]] = []
        for case in observational_cases:
            print(f"\n[case] {case.query}")
            if case.note:
                print(f"  note={case.note}")

            normalized_query = build_case_normalized_query(case)
            full_prefixed = build_case_query_for_cap(
                case,
                max_chars=rag_query_max_chars,
                query_prefix=rag_query_prefix,
                rewrite_enabled=rewrite_enabled,
            )
            full_unprefixed = build_case_query_for_cap(
                case,
                max_chars=rag_query_max_chars,
                query_prefix="",
                rewrite_enabled=rewrite_enabled,
            )

            split_prefixed = build_split_subqueries(
                normalized_query,
                query_prefix=rag_query_prefix,
                max_chars=rag_query_max_chars,
            )
            split_unprefixed = build_split_subqueries(
                normalized_query,
                query_prefix="",
                max_chars=rag_query_max_chars,
            )

            variant_queries: list[tuple[str, str]] = [
                ("full_prefixed", full_prefixed),
                ("full_unprefixed", full_unprefixed),
            ]
            variant_queries.extend(
                (f"split_prefixed[{index}]", query)
                for index, query in enumerate(split_prefixed, start=1)
            )
            variant_queries.extend(
                (f"split_unprefixed[{index}]", query)
                for index, query in enumerate(split_unprefixed, start=1)
            )
            variant_queries.extend(
                (f"diagnose_extra[{index}]", query)
                for index, query in enumerate(case.diagnose_queries, start=1)
            )

            split_prefixed_rows: list[list[dict[str, Any]]] = []
            split_unprefixed_rows: list[list[dict[str, Any]]] = []
            for label, query in variant_queries:
                rows = _search(query, safe_depth)
                if label.startswith("split_prefixed"):
                    split_prefixed_rows.append(rows)
                if label.startswith("split_unprefixed"):
                    split_unprefixed_rows.append(rows)

                found_top_k, found_top_depth = _variant_line(
                    label=label,
                    query=query,
                    rows=rows,
                    expected=case.expected_sources,
                    retrieval_calls=1,
                )
                table_rows.append((case.query, label, found_top_k, found_top_depth))

            if split_prefixed_rows:
                fused_prefixed_all = reciprocal_rank_fuse(
                    split_prefixed_rows,
                    top_k=safe_depth,
                    rrf_k=60,
                )
                fused_prefixed = fused_prefixed_all[:safe_top_k]
                prefixed_pass = (
                    "PASS"
                    if count_expected_sources_found(
                        fused_prefixed,
                        case.expected_sources,
                        limit=safe_top_k,
                    )
                    > 0
                    else "FAIL"
                )
                found_top_k, found_top_depth = _variant_line(
                    label=f"fused_split_prefixed({prefixed_pass})",
                    query="<rrf split-prefixed>",
                    rows=fused_prefixed_all,
                    expected=case.expected_sources,
                    retrieval_calls=len(split_prefixed_rows),
                )
            else:
                print("  [fused_split_prefixed(n/a)] query='<rrf split-prefixed>' calls=0")
                found_top_k, found_top_depth = (0, 0)
            table_rows.append(
                (case.query, "fused_split_prefixed", found_top_k, found_top_depth)
            )

            if split_unprefixed_rows:
                fused_unprefixed_all = reciprocal_rank_fuse(
                    split_unprefixed_rows,
                    top_k=safe_depth,
                    rrf_k=60,
                )
                fused_unprefixed = fused_unprefixed_all[:safe_top_k]
                unprefixed_pass = (
                    "PASS"
                    if count_expected_sources_found(
                        fused_unprefixed,
                        case.expected_sources,
                        limit=safe_top_k,
                    )
                    > 0
                    else "FAIL"
                )
                found_top_k, found_top_depth = _variant_line(
                    label=f"fused_split_unprefixed({unprefixed_pass})",
                    query="<rrf split-unprefixed>",
                    rows=fused_unprefixed_all,
                    expected=case.expected_sources,
                    retrieval_calls=len(split_unprefixed_rows),
                )
            else:
                print("  [fused_split_unprefixed(n/a)] query='<rrf split-unprefixed>' calls=0")
                found_top_k, found_top_depth = (0, 0)
            table_rows.append(
                (case.query, "fused_split_unprefixed", found_top_k, found_top_depth)
            )

        print("\ndiagnose_table: case | variant | expected@top_k | expected@top_depth")
        for case_query, variant, found_top_k, found_top_depth in table_rows:
            print(
                f"- {case_query} | {variant} | {found_top_k} | {found_top_depth}"
            )
        return 0

    eval_cases = [
        replace(
            case,
            query=build_case_query_for_cap(
                case,
                max_chars=rag_query_max_chars,
                query_prefix=rag_query_prefix,
                rewrite_enabled=rewrite_enabled,
            ),
        )
        for case in cases
    ]

    results = evaluate_cases(cases=eval_cases, search_fn=_search, top_k=args.top_k)
    summary = summarize_results(results)

    cap_values: list[int] = []
    for token in str(args.query_max_chars).split(","):
        token = token.strip()
        if token == "":
            continue
        cap_values.append(max(80, int(token)))
    if not cap_values:
        cap_values = [200, 300, 500]

    variants = parse_query_variants(args.query_variant)

    def _hits_for(query: str) -> list[dict[str, Any]]:
        return _search(query, args.top_k)

    def _print_hits(rows: list[dict[str, Any]]) -> None:
        if not rows:
            print("  (no hits)")
            return
        for index, hit in enumerate(rows[: max(1, args.top_k)], start=1):
            metadata = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
            section = metadata.get("section") if isinstance(metadata.get("section"), str) else None
            chunk_index = (
                metadata.get("chunk_index")
                if isinstance(metadata.get("chunk_index"), int)
                else None
            )
            score = hit.get("score") if isinstance(hit.get("score"), float) else None
            source = str(hit.get("source") or "unknown")
            print(
                f"  {index}. source={source} score={score} "
                f"section={section} chunk_index={chunk_index}"
            )

    def _passed(rows: list[dict[str, Any]], expected_sources: tuple[str, ...]) -> bool:
        expected = {value.strip() for value in expected_sources}
        return any(str(row.get("source") or "") in expected for row in rows[: max(1, args.top_k)])

    split_required_total = 0
    split_required_passed = 0
    split_optional_total = 0
    split_optional_passed = 0

    print(f"namespace={args.namespace}")
    for result in results:
        case = result.case
        flag = "PASS" if result.passed else "FAIL"
        required_tag = "required" if case.required else "observational"
        print(f"\n[{flag}] ({required_tag}) query: {case.query}")
        print(f"expected: {', '.join(case.expected_sources)}")
        print(f"final query: {case.query}")
        if case.note:
            print(f"note: {case.note}")
        if case.query_raw and case.query_normalized:
            raw_hits = _hits_for(case.query_raw)
            normalized_query = build_case_query_for_cap(
                case,
                max_chars=rag_query_max_chars,
                query_prefix=rag_query_prefix,
                rewrite_enabled=rewrite_enabled,
            )
            normalized_hits = _hits_for(normalized_query)
            raw_flag = "PASS" if _passed(raw_hits, case.expected_sources) else "FAIL"
            norm_flag = "PASS" if _passed(normalized_hits, case.expected_sources) else "FAIL"

            print("raw query:")
            print(f"  {case.query_raw}")
            print(f"  result={raw_flag}")
            _print_hits(raw_hits)

            print("normalized query:")
            print(f"  {normalized_query}")
            print(f"  result={norm_flag}")
            _print_hits(normalized_hits)

            print("normalized caps:")
            for cap in cap_values:
                capped_base = build_case_query_for_cap(
                    case,
                    max_chars=cap,
                    query_prefix="",
                    rewrite_enabled=rewrite_enabled,
                )
                print(f"  cap={cap} base_query={capped_base}")
                for variant in variants:
                    variant_query = apply_query_variant(capped_base, variant)
                    variant_hits = _hits_for(variant_query)
                    variant_flag = (
                        "PASS" if _passed(variant_hits, case.expected_sources) else "FAIL"
                    )
                    top1_score = (
                        variant_hits[0].get("score")
                        if variant_hits and isinstance(variant_hits[0].get("score"), float)
                        else None
                    )
                    print(
                        f"    variant={variant} result={variant_flag} "
                        f"top1_score={top1_score}"
                    )
                    print(f"      query={variant_query}")
                    _print_hits(variant_hits)
        else:
            _print_hits(
                [
                    {
                        "source": hit.source,
                        "score": hit.score,
                        "metadata": {
                            "section": hit.section,
                            "chunk_index": hit.chunk_index,
                        },
                    }
                    for hit in result.top_hits
                ]
            )

        if args.split_queries:
            base_rows = [
                {
                    "source": hit.source,
                    "score": hit.score,
                    "metadata": {
                        "section": hit.section,
                        "chunk_index": hit.chunk_index,
                    },
                }
                for hit in result.top_hits
            ]
            normalized_query = build_case_normalized_query(case)
            sub_queries = build_split_subqueries(
                normalized_query,
                query_prefix=rag_query_prefix,
                max_chars=rag_query_max_chars,
            )

            retrieval_calls = 1
            split_lists: list[list[dict[str, Any]]] = [base_rows]

            print("split-query experiment:")
            print(f"  full_query={case.query}")
            print("  full_query_hits:")
            _print_hits(base_rows)

            if sub_queries:
                for idx, sub_query in enumerate(sub_queries, start=1):
                    sub_hits = _hits_for(sub_query)
                    retrieval_calls += 1
                    split_lists.append(sub_hits)
                    print(f"  sub_query[{idx}]={sub_query}")
                    _print_hits(sub_hits)
            else:
                print("  sub_query: skipped (fewer than 2 eligible segments)")

            fused_hits = reciprocal_rank_fuse(split_lists, top_k=args.top_k, rrf_k=60)
            split_flag = "PASS" if _passed(fused_hits, case.expected_sources) else "FAIL"
            print("  fused_hits:")
            _print_hits(fused_hits)
            print(f"  non_split_result={flag}")
            print(f"  split_result={split_flag}")
            print(f"  retrieval_calls={retrieval_calls}")

            if case.required:
                split_required_total += 1
                if split_flag == "PASS":
                    split_required_passed += 1
            else:
                split_optional_total += 1
                if split_flag == "PASS":
                    split_optional_passed += 1

    print(
        "\nsummary: "
        f"required_passed={summary['required_passed']}/{summary['required_total']} "
        f"observational_passed={summary['optional_passed']}/{summary['optional_total']}"
    )

    strict_total_expected = sum(len(result.case.expected_sources) for result in results)
    strict_found_expected = sum(
        count_expected_sources_found(
            [
                {
                    "source": hit.source,
                    "score": hit.score,
                    "metadata": {
                        "section": hit.section,
                        "chunk_index": hit.chunk_index,
                    },
                }
                for hit in result.top_hits
            ],
            result.case.expected_sources,
            limit=safe_top_k,
        )
        for result in results
    )
    print(
        "strict_coverage_top_k: "
        f"{strict_found_expected}/{strict_total_expected}"
    )
    if args.split_queries:
        print(
            "split_summary: "
            f"required non_split={summary['required_passed']}/{summary['required_total']} "
            f"split={split_required_passed}/{split_required_total} "
            f"observational non_split={summary['optional_passed']}/{summary['optional_total']} "
            f"split={split_optional_passed}/{split_optional_total}"
        )

    if args.json_out is not None:
        payload = {
            "namespace": args.namespace,
            "summary": summary,
            "results": [
                {
                    "query": result.case.query,
                    "query_raw": result.case.query_raw,
                    "query_normalized": result.case.query_normalized,
                    "required": result.case.required,
                    "expected_sources": list(result.case.expected_sources),
                    "note": result.case.note,
                    "passed": result.passed,
                    "top_hits": [
                        {
                            "source": hit.source,
                            "score": hit.score,
                            "section": hit.section,
                            "chunk_index": hit.chunk_index,
                        }
                        for hit in result.top_hits
                    ],
                }
                for result in results
            ],
        }
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    if summary["required_passed"] != summary["required_total"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
