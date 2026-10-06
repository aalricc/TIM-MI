from __future__ import annotations

from mi_agent.agent.agent import MIAgent


def test_normalize_db_file_missing_from_verbose_phrase() -> None:
    raw = (
        "Persistent SQLite datastore unavailability (missing_db/schema_files) "
        "causing startup instability and DB check failures"
    )
    assert MIAgent._normalize_failure_type(raw) == "db_file_missing"


def test_normalize_db_file_missing_from_known_error_text() -> None:
    raw = "sqlite3.OperationalError: unable to open database file"
    assert MIAgent._normalize_failure_type(raw) == "db_file_missing"


def test_normalize_storage_initialization_to_db_file_missing() -> None:
    raw = "database_initialization_storage_misconfiguration"
    assert MIAgent._normalize_failure_type(raw) == "db_file_missing"


def test_normalize_long_unknown_failure_type_is_bounded_and_stable() -> None:
    raw = "Very " + ("unexpected token stream " * 20)
    key1 = MIAgent._normalize_failure_type(raw)
    key2 = MIAgent._normalize_failure_type(raw)

    assert key1 == key2
    assert len(key1) <= 64
    assert key1 != "unclassified_failure"


def test_normalize_empty_falls_back_to_unclassified() -> None:
    assert MIAgent._normalize_failure_type("  ") == "unclassified_failure"
