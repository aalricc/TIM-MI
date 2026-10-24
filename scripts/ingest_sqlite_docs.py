#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any


def _ensure_src_on_path() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    src_path = repo_root / "src"
    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))


_ensure_src_on_path()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest SQLite docs PDFs into Pinecone")
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("MI/knowledge_base/raw"),
        help="Directory containing source PDFs",
    )
    parser.add_argument(
        "--namespace",
        default="sqlite-docs-v2",
        help="Target Pinecone namespace",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and print records without writing to Pinecone",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Delete all records from target namespace before upsert",
    )
    parser.add_argument(
        "--allow-live",
        action="store_true",
        help="Allow writing to live namespace sqlite-docs",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=3,
        help="How many cleaned samples and record previews to print",
    )
    return parser.parse_args()


def main() -> int:
    from mi_agent.rag_ingest import (
        chunk_document,
        chunk_text_document,
        dedupe_records,
        delete_namespace_records,
        ensure_write_allowed,
        extract_pdf_pages,
        to_upsert_record,
        upsert_records,
    )

    args = _parse_args()
    ensure_write_allowed(namespace=args.namespace, allow_live=args.allow_live, dry_run=args.dry_run)

    input_paths = sorted(
        path
        for path in args.input_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in {".pdf", ".md", ".txt"}
    )
    if not input_paths:
        print(f"No .pdf/.md/.txt files found in {args.input_dir}")
        return 0

    all_records: list[dict[str, Any]] = []
    cleanup_samples: list[tuple[str, str]] = []
    source_mappings: list[tuple[str, str]] = []

    for input_path in input_paths:
        source_file = input_path.stem
        source_mappings.append((str(input_path.relative_to(args.input_dir)), source_file))

        if input_path.suffix.lower() == ".pdf":
            pages = extract_pdf_pages(input_path)
            records, samples = chunk_document(source_file=source_file, pages=pages)
        else:
            text = input_path.read_text(encoding="utf-8", errors="ignore")
            records, samples = chunk_text_document(source_file=source_file, text=text)

        all_records.extend(records)
        for sample in samples:
            if len(cleanup_samples) < max(1, args.sample_limit):
                cleanup_samples.append(sample)

    deduped = dedupe_records(all_records)
    counts = Counter(str(item.get("source_file") or "unknown") for item in deduped)
    section_sources: dict[str, set[str]] = {}
    for item in deduped:
        section = str(item.get("section") or "").strip()
        if section == "":
            continue
        source = str(item.get("source_file") or "unknown")
        section_sources.setdefault(section, set()).add(source)

    print("Chunk counts per source_file:")
    for source_file in sorted(counts):
        print(f"  {source_file}: {counts[source_file]}")

    print("\nFile -> source_file mapping:")
    for relative_path, source_file in source_mappings:
        print(f"  {relative_path} -> {source_file}")

    shared_sections = [
        (section, len(sources), sorted(sources))
        for section, sources in section_sources.items()
        if len(sources) >= 3
    ]
    shared_sections.sort(key=lambda row: (-row[1], row[0].lower()))
    print("\nSections present in 3+ source_files:")
    if shared_sections:
        for section, source_count, sources in shared_sections:
            print(f"  {section} [{source_count} sources] -> {', '.join(sources)}")
    else:
        print("  (none)")

    long_sections = sorted(
        (
            section,
            len(section),
            len(sources),
            sorted(sources),
        )
        for section, sources in section_sources.items()
        if len(section) > 60
    )
    print("\nSections longer than 60 characters:")
    if long_sections:
        for section, section_len, source_count, sources in long_sections:
            print(f"  ({section_len}) {section} [{source_count} sources] -> {', '.join(sources)}")
    else:
        print("  (none)")

    rng = random.Random(42)  # noqa: S311 - seeded sampling for a report, not security
    sample_sources = [
        "Result and Error Codes",
        "Database File Format",
        "Pragma statements supported by SQLite",
    ]
    print("\nRandom section names (10 per source):")
    for sample_source in sample_sources:
        source_sections = sorted(
            {
                str(item.get("section") or "").strip()
                for item in deduped
                if str(item.get("source_file") or "") == sample_source
                and str(item.get("section") or "").strip()
            }
        )
        print(f"  {sample_source}:")
        if not source_sections:
            print("    (none)")
            continue
        if len(source_sections) <= 10:
            chosen = source_sections
        else:
            chosen = sorted(rng.sample(source_sections, 10))
        for section in chosen:
            print(f"    - {section}")

    if cleanup_samples:
        print("\nCleaned text samples (before -> after):")
        for before, after in cleanup_samples[: args.sample_limit]:
            print(f"  BEFORE: {before}")
            print(f"  AFTER:  {after}")
    else:
        print("\nCleaned text samples: none")

    preview = [to_upsert_record(item) for item in deduped[: args.sample_limit]]
    print("\nSample records:")
    print(json.dumps(preview, indent=2, ensure_ascii=False))

    if args.dry_run:
        print("\nDry-run mode: no upsert performed")
        print(f"records_ready={len(deduped)}")
        return 0

    api_key = os.getenv("PINECONE_API_KEY")
    index_host = os.getenv("PINECONE_INDEX_HOST")
    if not api_key or not index_host:
        print("PINECONE_API_KEY and PINECONE_INDEX_HOST are required", file=sys.stderr)
        return 2

    try:
        from pinecone import Pinecone  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise RuntimeError("pinecone package is required for ingestion") from exc

    index = Pinecone(api_key=api_key).Index(host=index_host)
    if args.rebuild:
        deleted = delete_namespace_records(index=index, namespace=args.namespace)
        if deleted:
            print(f"namespace_wiped={args.namespace}")
        else:
            print(f"namespace_missing_skip_delete={args.namespace}")

    upserted = upsert_records(index=index, namespace=args.namespace, records=deduped)
    print(f"upserted={upserted} namespace={args.namespace}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
