from __future__ import annotations

import importlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


def build_index(*, api_key: str, index_host: str) -> Any:
    try:
        pinecone_module = importlib.import_module("pinecone")
    except ModuleNotFoundError as exc:
        raise RuntimeError("pinecone package is required for namespace export") from exc
    Pinecone = getattr(pinecone_module, "Pinecone")
    return Pinecone(api_key=api_key).Index(host=index_host)


def export_namespace_records(
    *,
    index: Any,
    namespace: str,
    out_dir: Path,
    source_file: str | None = None,
    fetch_batch_size: int = 100,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)

    ids = list_namespace_ids(index=index, namespace=namespace)
    records = fetch_namespace_records(
        index=index,
        namespace=namespace,
        ids=ids,
        fetch_batch_size=fetch_batch_size,
    )
    records.sort(key=lambda item: str(item.get("_id") or ""))

    backup_path = out_dir / f"{namespace}_backup.jsonl"
    with backup_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    markdown_path: Path | None = None
    if source_file:
        filtered = [item for item in records if str(item.get("source_file") or "") == source_file]
        filtered.sort(
            key=lambda item: (_as_int(item.get("chunk_index")), str(item.get("_id") or ""))
        )

        markdown_path = out_dir / f"{source_file}.md"
        with markdown_path.open("w", encoding="utf-8") as handle:
            texts = [str(item.get("text") or "") for item in filtered]
            handle.write("\n\n".join(texts))

    counts = Counter(str(item.get("source_file") or "<missing>") for item in records)
    stats_total = describe_namespace_count(index=index, namespace=namespace)

    return {
        "backup_path": backup_path,
        "markdown_path": markdown_path,
        "records": records,
        "counts": dict(sorted(counts.items())),
        "exported_total": len(records),
        "stats_total": stats_total,
        "totals_match": stats_total is None or stats_total == len(records),
    }


def list_namespace_ids(*, index: Any, namespace: str) -> list[str]:
    all_ids: list[str] = []
    token: str | None = None
    seen_tokens: set[str] = set()

    while True:
        kwargs: dict[str, Any] = {"namespace": namespace}
        if token:
            kwargs["pagination_token"] = token

        page = index.list(**kwargs)
        if _is_page_iterator(page):
            for item in page:
                item_ids, _ = _extract_ids_and_token(item)
                all_ids.extend(item_ids)
            break

        page_ids, next_token = _extract_ids_and_token(page)
        all_ids.extend(page_ids)
        if not next_token:
            break
        if next_token in seen_tokens:
            break
        seen_tokens.add(next_token)
        token = next_token

    return sorted(set(all_ids))


def fetch_namespace_records(
    *,
    index: Any,
    namespace: str,
    ids: list[str],
    fetch_batch_size: int,
) -> list[dict[str, Any]]:
    if fetch_batch_size < 1:
        raise ValueError("fetch_batch_size must be >= 1")

    records: list[dict[str, Any]] = []
    for offset in range(0, len(ids), fetch_batch_size):
        batch_ids = ids[offset : offset + fetch_batch_size]
        response = index.fetch(ids=batch_ids, namespace=namespace)
        records.extend(_normalize_fetch_response(response=response, fallback_ids=batch_ids))
    return records


def describe_namespace_count(*, index: Any, namespace: str) -> int | None:
    stats = _as_dict(index.describe_index_stats())
    namespaces = stats.get("namespaces")
    if isinstance(namespaces, dict):
        namespace_stats = namespaces.get(namespace)
        namespace_stats_map = _as_dict(namespace_stats)
        count = namespace_stats_map.get("record_count")
        if isinstance(count, int):
            return count
        count = namespace_stats_map.get("vector_count")
        if isinstance(count, int):
            return count
    return None


def _normalize_fetch_response(*, response: Any, fallback_ids: list[str]) -> list[dict[str, Any]]:
    response_map = _as_dict(response)
    container = response_map.get("records")
    if not isinstance(container, dict):
        container = response_map.get("vectors")
    if not isinstance(container, dict):
        return []

    normalized: list[dict[str, Any]] = []
    for record_id in fallback_ids:
        raw = container.get(record_id)
        if raw is None:
            continue
        payload = _as_dict(raw)
        record: dict[str, Any] = {"_id": record_id}

        fields = payload.get("fields")
        if isinstance(fields, dict):
            record.update(fields)

        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            record.update(metadata)

        for key, value in payload.items():
            if key in {"fields", "metadata", "id", "values", "sparse_values", "sparseValues"}:
                continue
            record.setdefault(key, value)

        normalized.append(record)
    return normalized


def _extract_ids_and_token(page: Any) -> tuple[list[str], str | None]:
    page_map = _as_dict(page)
    ids: list[str] = []

    direct_ids = page_map.get("ids")
    if isinstance(direct_ids, list):
        ids.extend(str(item) for item in direct_ids)

    for key in ("records", "vectors"):
        value = page_map.get(key)
        if isinstance(value, dict):
            ids.extend(str(item) for item in value)

    next_token = page_map.get("pagination_token")
    if not isinstance(next_token, str) or next_token == "":
        pagination = page_map.get("pagination")
        pagination_map = _as_dict(pagination)
        maybe_next = pagination_map.get("next")
        next_token = maybe_next if isinstance(maybe_next, str) and maybe_next else None

    return ids, next_token


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 10**9


def _is_page_iterator(value: Any) -> bool:
    if isinstance(value, (dict, list, tuple, str, bytes)):
        return False
    return hasattr(value, "__iter__")


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        maybe = model_dump()
        if isinstance(maybe, dict):
            return maybe
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        maybe = to_dict()
        if isinstance(maybe, dict):
            return maybe
    return {}

