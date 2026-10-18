from __future__ import annotations

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

    try:
        from pinecone import Pinecone  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Pinecone dependency is missing. Install optional dependency group 'rag'."
        ) from exc

    client = Pinecone(api_key=api_key)
    index = client.Index(host=index_host)
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
                    "chunk_index": fields.get("chunk_index"),
                },
            }
        )
    return output
