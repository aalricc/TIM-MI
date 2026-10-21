from __future__ import annotations

from typing import Any

from mi_agent.sqlite_docs_tool import search_sqlite_docs


class _FakeIndex:
    def search(self, *, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        del namespace, query
        return {
            "result": {
                "hits": [
                    {
                        "_score": 0.91,
                        "fields": {
                            "source_file": "doc-a",
                            "text": "chunk a",
                            "section": "intro",
                            "chunk_index": 3.0,
                        },
                    },
                    {
                        "_score": 0.52,
                        "fields": {
                            "source_file": "doc-b",
                            "text": "chunk b",
                            "section": "details",
                            "chunk_index": "4",
                        },
                    },
                ]
            }
        }


def test_search_sqlite_docs_coerces_chunk_index(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "mi_agent.sqlite_docs_tool._cached_index",
        lambda api_key, index_host: _FakeIndex(),
    )

    rows = search_sqlite_docs(
        "database is locked",
        3,
        api_key="test-key",
        index_host="test-host",
        namespace="sqlite-docs",
    )

    assert rows[0]["metadata"]["chunk_index"] == 3
    assert rows[1]["metadata"]["chunk_index"] == 4
