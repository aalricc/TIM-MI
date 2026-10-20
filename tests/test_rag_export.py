from __future__ import annotations

import json
from pathlib import Path

from mi_agent.rag_export import export_namespace_records


class _FakeIndex:
    def __init__(self) -> None:
        self._pages = [
            {"ids": ["a", "b"], "pagination_token": "next-1"},
            {"ids": ["c"], "pagination_token": None},
        ]
        self._fetch_data = {
            "a": {
                "metadata": {
                    "source_file": "sqlite_error_message_glossary",
                    "chunk_index": 1,
                    "text": "(11) SQLITE_CORRUPT ...",
                }
            },
            "b": {
                "metadata": {
                    "source_file": "sqlite_error_message_glossary",
                    "chunk_index": 0,
                    "text": "(5) SQLITE_BUSY ...",
                }
            },
            "c": {
                "metadata": {
                    "source_file": "Write-Ahead Logging",
                    "chunk_index": 2,
                    "text": "WAL details",
                }
            },
        }

    def list(self, *, namespace: str, pagination_token: str | None = None):
        if pagination_token is None:
            return self._pages[0]
        return self._pages[1]

    def fetch(self, *, ids: list[str], namespace: str):
        return {
            "vectors": {item: self._fetch_data[item] for item in ids if item in self._fetch_data}
        }

    def describe_index_stats(self):
        return {"namespaces": {"sqlite-docs": {"record_count": 3}}}


def test_export_namespace_records_writes_backup_and_filtered_markdown(tmp_path: Path) -> None:
    index = _FakeIndex()
    result = export_namespace_records(
        index=index,
        namespace="sqlite-docs",
        out_dir=tmp_path,
        source_file="sqlite_error_message_glossary",
        fetch_batch_size=2,
    )

    backup_path = tmp_path / "sqlite-docs_backup.jsonl"
    assert backup_path.exists()
    rows = [json.loads(line) for line in backup_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 3
    assert rows[0]["_id"] == "a"
    assert result["exported_total"] == 3
    assert result["stats_total"] == 3
    assert result["totals_match"] is True

    markdown_path = tmp_path / "sqlite_error_message_glossary.md"
    assert markdown_path.exists()
    chunks = markdown_path.read_text(encoding="utf-8").split("\n\n")
    assert chunks[0].startswith("(5) SQLITE_BUSY")
    assert chunks[1].startswith("(11) SQLITE_CORRUPT")

