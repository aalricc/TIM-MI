from __future__ import annotations

from pathlib import Path

import pytest

from mi_agent.failure_taxonomy import FailureTaxonomy

pytestmark = pytest.mark.phase5


def test_taxonomy_merges_near_duplicates_by_similarity(tmp_path: Path) -> None:
    taxonomy = FailureTaxonomy(
        tmp_path / "taxonomy.json",
        merge_threshold=0.9,
        review_threshold=0.7,
    )

    first = taxonomy.resolve("db_file_missing")
    second = taxonomy.resolve("db_file_missng")

    assert first == "db_file_missing"
    assert second == "db_file_missing"
    assert taxonomy.review_queue() == []


def test_taxonomy_queues_low_confidence_candidates(tmp_path: Path) -> None:
    taxonomy = FailureTaxonomy(
        tmp_path / "taxonomy.json",
        merge_threshold=0.9,
        review_threshold=0.5,
    )

    taxonomy.resolve("db_file_missing")
    resolved = taxonomy.resolve("dbfile_missing_variant")

    assert resolved == "dbfile_missing_variant"
    queue = taxonomy.review_queue()
    assert len(queue) == 1
    assert queue[0]["candidate"] == "dbfile_missing_variant"
    assert queue[0]["nearest"] == "db_file_missing"
