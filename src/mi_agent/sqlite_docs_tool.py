from __future__ import annotations

import importlib
from functools import lru_cache
from typing import Any


def search_sqlite_docs(
    query: str,
    k: int,
    *,
    api_key: str,
    index_host: str,
    namespace: str,
) -> list[dict[str, Any]]:
    if query.strip() == "":
        return []

    index = _cached_index(api_key, index_host)
    result = index.search(
        namespace=namespace,
        query={"top_k": k, "inputs": {"text": query}},
    )

    hits = result.get("result", {}).get("hits", [])
    output: list[dict[str, Any]] = []
    for hit in hits:
        fields = hit.get("fields", {})
        output.append(
            {
                "source": str(fields.get("source_file") or "unknown"),
                "text": str(fields.get("text") or ""),
                "score": float(hit.get("_score")) if hit.get("_score") is not None else None,
                "metadata": {
                    "section": fields.get("section"),
                    "chunk_index": _coerce_chunk_index(fields.get("chunk_index")),
                },
            }
        )
    return output


@lru_cache(maxsize=16)
def _cached_index(api_key: str, index_host: str) -> Any:
    try:
        pinecone_module = importlib.import_module("pinecone")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Pinecone dependency is missing. Install optional dependency group 'rag'."
        ) from exc
    Pinecone = pinecone_module.Pinecone

    client = Pinecone(api_key=api_key)
    return client.Index(host=index_host)


def _coerce_chunk_index(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        return int(number) if number.is_integer() else None
    return None
