#!/usr/bin/env python
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _ensure_src_on_path() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    src_path = repo_root / "src"
    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))


_ensure_src_on_path()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only export from a Pinecone namespace")
    parser.add_argument("--namespace", default="sqlite-docs", help="Source namespace")
    parser.add_argument(
        "--source-file",
        default=None,
        help="Optional source_file filter for markdown export",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("knowledge_base/exports"),
        help="Export output directory",
    )
    parser.add_argument(
        "--fetch-batch-size",
        type=int,
        default=100,
        help="Number of IDs per fetch request",
    )
    return parser.parse_args()


def main() -> int:
    from mi_agent.rag_export import build_index, export_namespace_records

    args = _parse_args()
    api_key = os.getenv("PINECONE_API_KEY")
    index_host = os.getenv("PINECONE_INDEX_HOST")
    if not api_key or not index_host:
        print("PINECONE_API_KEY and PINECONE_INDEX_HOST are required", file=sys.stderr)
        return 2

    index = build_index(api_key=api_key, index_host=index_host)
    result = export_namespace_records(
        index=index,
        namespace=args.namespace,
        out_dir=args.out_dir,
        source_file=args.source_file,
        fetch_batch_size=max(1, args.fetch_batch_size),
    )

    print(f"backup_file={result['backup_path']}")
    if result["markdown_path"] is not None:
        print(f"source_file_markdown={result['markdown_path']}")

    print("record_count_per_source_file:")
    counts = result["counts"]
    for source_file in sorted(counts):
        print(f"  {source_file}: {counts[source_file]}")

    print(
        "totals: "
        f"exported={result['exported_total']} "
        f"index_stats={result['stats_total']} "
        f"match={result['totals_match']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

