from __future__ import annotations

from pathlib import Path

from mi_agent.agent.engine import RecoveryAgent
from mi_agent.events import EventLog
from mi_agent.workflow.models import Diagnosis, IncidentContext


def _agent(
    tmp_path: Path,
    *,
    prefix: str = "sqlite",
    max_chars: int = 300,
) -> RecoveryAgent:
    return RecoveryAgent(
        llm=object(),  # type: ignore[arg-type]
        tools=object(),  # type: ignore[arg-type]
        verifier=object(),  # type: ignore[arg-type]
        events=EventLog(tmp_path / "events.jsonl"),
        policy_store_path=(tmp_path / "policy.json").as_posix(),
        monitor_interval_seconds=0.1,
        rag_query_prefix=prefix,
        rag_query_max_chars=max_chars,
    )


def test_default_prefix_applies_to_symptom_and_repair_queries(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    diagnostics = {"health_deep": {"error": "database disk image is malformed"}}

    symptom_query = agent._build_symptom_query(diagnostics)
    assert symptom_query.startswith("sqlite | ")

    incident = IncidentContext(
        incident_id="inc-prefix",
        diagnosis=Diagnosis(failure_type="db_corrupt", hypotheses=["btree issue"]),
        latest_diagnostics=diagnostics,
        last_tool_error="run_sql failed",
    )
    repair_query = agent._build_repair_query(incident)
    assert repair_query.startswith("sqlite | ")


def test_empty_prefix_disables_prepend_and_no_double_prefix(tmp_path: Path) -> None:
    agent_no_prefix = _agent(tmp_path, prefix="   ")
    diagnostics = {"health_deep": {"error": "database disk image is malformed"}}
    no_prefix_query = agent_no_prefix._build_symptom_query(diagnostics)
    assert not no_prefix_query.startswith("sqlite | ")
    assert "database disk image is malformed" in no_prefix_query

    agent_with_prefix = _agent(tmp_path, prefix="sqlite")
    already_prefixed = {
        "health_deep": {"error": "SQLite | database disk image is malformed"},
    }
    prefixed_query = agent_with_prefix._build_symptom_query(already_prefixed)
    assert prefixed_query.lower().count("sqlite |") == 1


def test_custom_prefix_override_still_works(tmp_path: Path) -> None:
    agent_with_override = _agent(tmp_path, prefix="sqlite recovery")
    diagnostics = {"health_deep": {"error": "database disk image is malformed"}}
    query = agent_with_override._build_symptom_query(diagnostics)
    assert query.startswith("sqlite recovery | ")


def test_cap_applies_after_prefixing(tmp_path: Path) -> None:
    agent = _agent(tmp_path, max_chars=120)
    diagnostics = {
        "health_deep": {
            "error": "database disk image is malformed while running checkpoint and writer "
            "with repeated retries and alternating read/write contention in prod",
        }
    }
    query = agent._build_symptom_query(diagnostics)
    assert query.startswith("sqlite")
    assert len(query) <= 120
