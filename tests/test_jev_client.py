from __future__ import annotations

import pytest

from mi_agent.config import MISettings
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


@pytest.mark.asyncio
async def test_choose_candidate_returns_unusable_on_transport_error() -> None:
    settings = MISettings(
        OPENAI_API_KEY="test-openai",
        JEV_API_KEY="test-jev",
        PINECONE_API_KEY="test-pinecone",
        PINECONE_INDEX_HOST="test-index",
    )
    client = JevDecisionClient(settings)

    def _boom(state: dict, candidates: list[dict]) -> None:
        del state, candidates
        raise RuntimeError("boom")

    client._choose_candidate_sync = _boom  # type: ignore[method-assign]
    decision = await client.choose_candidate(
        state={"incident_id": "inc-1"},
        candidates=[{"name": "x", "operation": "run_sql", "rationale": "r", "params": {}}],
    )

    assert decision.usable is False
    assert decision.reason == "jev_request_error:RuntimeError"
    assert decision.raw.get("exception_type") == "RuntimeError"
    state_summary = decision.raw.get("state_summary")
    assert isinstance(state_summary, dict)
    assert state_summary.get("keys") == ["incident_id"]


@pytest.mark.asyncio
async def test_choose_candidate_includes_typesafe_422_diagnostics() -> None:
    settings = MISettings(
        OPENAI_API_KEY="test-openai",
        JEV_API_KEY="test-jev",
        PINECONE_API_KEY="test-pinecone",
        PINECONE_INDEX_HOST="test-index",
    )
    client = JevDecisionClient(settings)

    class _FakeTypeSafe422(Exception):
        def __init__(self) -> None:
            self.status = 422
            self.endpoint = "POST http://host.docker.internal:8017/v1/systemone"
            self.request_id = "req_123"
            self.body = {
                "detail": [
                    {
                        "type": "extra_forbidden",
                        "loc": ["body", "state", "class_memory_candidate"],
                        "msg": "Extra inputs are not permitted",
                        "input": {"sensitive": "drop"},
                    }
                ]
            }
            super().__init__("422 Unprocessable Entity")

    def _boom(state: dict, candidates: list[dict]) -> None:
        del state, candidates
        raise _FakeTypeSafe422()

    client._choose_candidate_sync = _boom  # type: ignore[method-assign]
    decision = await client.choose_candidate(
        state={
            "incident_id": "inc-1",
            "source": "class_memory",
            "class_memory_candidate": {"class_id": "abc", "lcb": 0.9},
            "candidate_actions": [{"name": "x"}],
        },
        candidates=[{"name": "x", "operation": "run_sql", "rationale": "r", "params": {}}],
    )

    assert decision.usable is False
    assert decision.reason == "jev_request_error:_FakeTypeSafe422"
    assert decision.raw.get("status") == 422
    assert decision.raw.get("endpoint") == "POST http://host.docker.internal:8017/v1/systemone"
    assert decision.raw.get("request_id") == "req_123"
    body = decision.raw.get("body")
    assert isinstance(body, dict)
    detail = body.get("detail")
    assert isinstance(detail, list)
    assert isinstance(detail[0], dict)
    assert "input" not in detail[0]
    state_summary = decision.raw.get("state_summary")
    assert isinstance(state_summary, dict)
    assert state_summary.get("class_memory_candidate_keys") == ["class_id", "lcb"]


def test_choose_candidate_sync_single_candidate_does_not_require_choice() -> None:
    pytest.importorskip("langchain_typesafe")
    settings = MISettings(
        OPENAI_API_KEY="test-openai",
        JEV_API_KEY="test-jev",
        PINECONE_API_KEY="test-pinecone",
        PINECONE_INDEX_HOST="test-index",
    )
    client = JevDecisionClient(settings)
    client._build_classifier = lambda _cls: object()  # type: ignore[method-assign]
    client._invoke_classifier = lambda _classifier, _payload: {  # type: ignore[method-assign]
        "nouls": {"can_execute_automatically": {"noul": 0.91}},
        "scores": {"risk_score": {"score": 0.12}},
        "model_version": "jev-test",
    }

    decision = client._choose_candidate_sync(
        state={"incident_id": "inc-1"},
        candidates=[
            {
                "name": "repair_sql",
                "operation": "run_sql",
                "rationale": "r",
                "params": {"db_path": "data/app.db", "sql": "SELECT 1;"},
            }
        ],
    )

    assert decision.usable is True
    assert decision.selected_candidate_name == "repair_sql"
    assert decision.selected_candidate_operation == "run_sql"
    assert decision.usable_probability == 0.91
    assert decision.risk_score == 0.12
    assert decision.shadow.get("selected_option") == "option_1"
    per_candidate = decision.shadow.get("per_candidate")
    assert isinstance(per_candidate, dict)
    assert "choice_margin" not in decision.shadow


def test_choose_candidate_sync_collects_shadow_metrics_for_multiple_candidates() -> None:
    pytest.importorskip("langchain_typesafe")
    settings = MISettings(
        OPENAI_API_KEY="test-openai",
        JEV_API_KEY="test-jev",
        PINECONE_API_KEY="test-pinecone",
        PINECONE_INDEX_HOST="test-index",
    )
    client = JevDecisionClient(settings)
    client._build_classifier = lambda _cls: object()  # type: ignore[method-assign]
    client._invoke_classifier = lambda _classifier, _payload: {  # type: ignore[method-assign]
        "choices": {
            "select_action": {
                "choice": "option_2",
                "probabilities": {"option_1": 0.35, "option_2": 0.65},
            }
        },
        "nouls": {
            "can_execute_automatically": {"noul": 0.91},
            "safe_option_1": {"noul": 0.25},
            "destructive_option_1": {"noul": 0.12},
            "safe_option_2": {"noul": 0.88},
            "destructive_option_2": {"noul": 0.08},
        },
        "scores": {"risk_score": {"score": 0.12}},
        "model_version": "jev-test",
    }

    decision = client._choose_candidate_sync(
        state={"incident_id": "inc-1"},
        candidates=[
            {
                "name": "restore",
                "operation": "request_backup_restore",
                "rationale": "recover db",
                "params": {"backup_id": "latest"},
            },
            {
                "name": "repair_sql",
                "operation": "run_sql",
                "rationale": "repair",
                "params": {"db_path": "data/app.db", "sql": "PRAGMA integrity_check;"},
            },
        ],
    )

    assert decision.usable is True
    assert decision.selected_candidate_name == "repair_sql"
    assert decision.selected_candidate_operation == "run_sql"

    per_candidate = decision.shadow.get("per_candidate")
    assert isinstance(per_candidate, dict)
    assert set(per_candidate.keys()) == {"option_1", "option_2"}
    assert per_candidate["option_1"]["safe"] == 0.25
    assert per_candidate["option_1"]["destructive"] == 0.12
    assert per_candidate["option_2"]["safe"] == 0.88
    assert per_candidate["option_2"]["destructive"] == 0.08
    assert decision.shadow.get("choice_margin") == pytest.approx(0.3)


def test_choose_candidate_sync_single_candidate_respects_threshold() -> None:
    pytest.importorskip("langchain_typesafe")
    settings = MISettings(
        OPENAI_API_KEY="test-openai",
        JEV_API_KEY="test-jev",
        PINECONE_API_KEY="test-pinecone",
        PINECONE_INDEX_HOST="test-index",
    )
    client = JevDecisionClient(settings)
    client._build_classifier = lambda _cls: object()  # type: ignore[method-assign]
    client._invoke_classifier = lambda _classifier, _payload: {  # type: ignore[method-assign]
        "nouls": {"can_execute_automatically": {"noul": 0.10}},
        "scores": {"risk_score": {"score": 0.42}},
    }

    decision = client._choose_candidate_sync(
        state={"incident_id": "inc-2"},
        candidates=[
            {
                "name": "repair_sql",
                "operation": "run_sql",
                "rationale": "r",
                "params": {"db_path": "data/app.db", "sql": "SELECT 1;"},
            }
        ],
    )

    assert decision.usable is False
    assert decision.reason is not None and decision.reason.startswith(
        "usable_probability_below_threshold:"
    )
    assert decision.selected_candidate_name == "repair_sql"
    assert decision.selected_candidate_operation == "run_sql"
    assert decision.shadow.get("selected_option") == "option_1"
    per_candidate = decision.shadow.get("per_candidate")
    assert isinstance(per_candidate, dict)
    assert set(per_candidate.keys()) == {"option_1"}
    assert "choice_margin" not in decision.shadow

