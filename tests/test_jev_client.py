from __future__ import annotations

from mi_agent.decision.client import JevDecisionClient


def test_normalized_base_url_handles_docker_localhost() -> None:
    assert (
        JevDecisionClient._normalized_base_url(
            "http://127.0.0.1:8017",
            in_docker=True,
        )
        == "http://host.docker.internal:8017"
    )
    assert (
        JevDecisionClient._normalized_base_url(
            "http://localhost:8017",
            in_docker=True,
        )
        == "http://host.docker.internal:8017"
    )


def test_normalized_base_url_keeps_non_local_hosts() -> None:
    assert (
        JevDecisionClient._normalized_base_url(
            "http://jev.internal:8017",
            in_docker=True,
        )
        == "http://jev.internal:8017"
    )


def test_normalized_base_url_returns_none_for_empty_value() -> None:
    assert JevDecisionClient._normalized_base_url("", in_docker=True) is None
    assert JevDecisionClient._normalized_base_url("   ", in_docker=False) is None
    assert JevDecisionClient._normalized_base_url(None, in_docker=False) is None

