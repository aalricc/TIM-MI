from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import mi_agent.rag_eval
import mi_agent.sqlite_docs_tool


def _load_script_module():
    script_path = Path("scripts/rag_eval.py")
    spec = importlib.util.spec_from_file_location("rag_eval_script", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_split_queries_flag_default_off() -> None:
    module = _load_script_module()
    args = module._parse_args([])
    assert args.split_queries is False
    assert args.diagnose is False
    assert args.depth == 10
    assert args.no_rewrite is False


def test_split_queries_flag_enabled() -> None:
    module = _load_script_module()
    args = module._parse_args(["--split-queries"])
    assert args.split_queries is True


def test_no_rewrite_flag_enabled() -> None:
    module = _load_script_module()
    args = module._parse_args(["--no-rewrite"])
    assert args.no_rewrite is True


def test_diagnose_depth_passes_through_to_retriever(
    monkeypatch,
) -> None:
    module = _load_script_module()

    calls: list[int] = []

    def _fake_search_sqlite_docs(
        query: str,
        k: int,
        *,
        api_key: str,
        index_host: str,
        namespace: str,
    ) -> list[dict[str, object]]:
        del query, api_key, index_host, namespace
        calls.append(k)
        return [{"source": "The Schema Table", "score": 0.9, "metadata": {"chunk_index": 1}}]

    fake_case = mi_agent.rag_eval.RAGEvalCase(
        query="integrity row says in database main page 5 | log says no such table widgets",
        expected_sources=("The Schema Table",),
        required=False,
        query_raw=(
            "sqlite recovery | symptom grounding | integrity row says *** in database main page 5"
            " | log_tail=no such table widgets"
        ),
    )

    monkeypatch.setattr(
        mi_agent.sqlite_docs_tool,
        "search_sqlite_docs",
        _fake_search_sqlite_docs,
    )
    monkeypatch.setattr(
        mi_agent.rag_eval,
        "load_eval_cases",
        lambda path: [fake_case],
    )
    monkeypatch.setenv("PINECONE_API_KEY", "pc-key")
    monkeypatch.setenv("PINECONE_INDEX_HOST", "pc-host")
    monkeypatch.setattr(
        sys,
        "argv",
        ["rag_eval.py", "--diagnose", "--depth", "7", "--top-k", "3"],
    )

    result = module.main()

    assert result == 0
    assert calls
    assert set(calls) == {7}


def test_diagnose_fused_variants_show_na_when_no_split_subqueries(
    monkeypatch,
    capsys,
) -> None:
    module = _load_script_module()

    def _fake_search_sqlite_docs(
        query: str,
        k: int,
        *,
        api_key: str,
        index_host: str,
        namespace: str,
    ) -> list[dict[str, object]]:
        del query, k, api_key, index_host, namespace
        return [{"source": "other", "score": 0.1, "metadata": {"chunk_index": 1}}]

    fake_case = mi_agent.rag_eval.RAGEvalCase(
        query="service timeout",
        expected_sources=("Result and Error Codes",),
        required=False,
        query_raw="service timeout",
    )

    monkeypatch.setattr(
        mi_agent.sqlite_docs_tool,
        "search_sqlite_docs",
        _fake_search_sqlite_docs,
    )
    monkeypatch.setattr(
        mi_agent.rag_eval,
        "load_eval_cases",
        lambda path: [fake_case],
    )
    monkeypatch.setenv("PINECONE_API_KEY", "pc-key")
    monkeypatch.setenv("PINECONE_INDEX_HOST", "pc-host")
    monkeypatch.setattr(sys, "argv", ["rag_eval.py", "--diagnose"])

    result = module.main()
    output = capsys.readouterr().out

    assert result == 0
    assert "fused_split_prefixed(n/a)" in output
    assert "fused_split_unprefixed(n/a)" in output
