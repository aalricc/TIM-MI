from __future__ import annotations

import os
from pathlib import Path

import pytest

from mi_agent.rag_eval import evaluate_cases, load_eval_cases, summarize_results
from mi_agent.sqlite_docs_tool import search_sqlite_docs


@pytest.mark.live
def test_live_rag_eval_top3_expected_sources() -> None:
    api_key = os.getenv("PINECONE_API_KEY")
    index_host = os.getenv("PINECONE_INDEX_HOST")
    if not api_key or not index_host:
        pytest.skip("PINECONE_API_KEY and PINECONE_INDEX_HOST are required")

    cases = load_eval_cases(Path("tests/data/rag_eval_cases.json"))

    def _search(query: str, k: int) -> list[dict[str, object]]:
        return search_sqlite_docs(
            query,
            k,
            api_key=api_key,
            index_host=index_host,
            namespace=os.getenv("PINECONE_NAMESPACE", "sqlite-docs"),
        )

    results = evaluate_cases(cases=cases, search_fn=_search, top_k=3)
    summary = summarize_results(results)
    assert summary["required_passed"] >= 1
