from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from mi_agent.api.server import create_api
from mi_agent.events.models import Event
from mi_agent.llm.client import FakeLLMClient
from mi_agent.policy.decision_log import DecisionLogEntry, RewardComponents
from mi_agent.runtime import build_agent

pytestmark = pytest.mark.phase7


async def _async_true() -> bool:
    return True


def test_phase7_observability_routes(test_settings) -> None:
    agent, context = build_agent(test_settings, FakeLLMClient())

    async def _noop() -> None:
        return None

    agent.start = _noop  # type: ignore[method-assign]
    agent.stop = _noop  # type: ignore[method-assign]

    context.decision_log.append(
        DecisionLogEntry(
            incident_id="inc-1",
            failure_type="db_file_missing",
            state_features={"probe_fingerprint": "abc", "attempt": "1"},
            candidate_set=["apply_schema|db_path='data/app.db',schema_path='schema.sql'"],
            chosen_action="apply_schema|db_path='data/app.db',schema_path='schema.sql'",
            propensities={"apply_schema|db_path='data/app.db',schema_path='schema.sql'": 1.0},
            policy_version="v1-tabular",
            verifier_healthy=True,
            reward_components=RewardComponents(net_reward=1.0),
        )
    )
    context.events._events.append(  # type: ignore[attr-defined]
        Event(
            event_type="agent.candidates_rejected_summary",
            payload={
                "incident_id": "inc-1",
                "failure_type": "db_file_missing",
                "state_fingerprint": "fp-1",
                "attempt_number": 1,
                "replan_attempt": 0,
                "cache_status": "miss",
                "plans_received": 1,
                "reason_counts": {
                    "first_step_not_executable": 2,
                    "invalid_parameters": 1,
                },
                "rejected_samples": [],
            },
        )
    )

    eval_dir = test_settings.state_dir / "eval_runs"
    eval_dir.mkdir(parents=True, exist_ok=True)
    (eval_dir / "run-1.json").write_text(
        json.dumps({"run_id": "run-1", "success_rate": 1.0}),
        encoding="utf-8",
    )

    app = create_api(context, agent)
    with TestClient(app) as client:
        metrics_response = client.get("/metrics")
        assert metrics_response.status_code == 200
        assert "mi_decisions_total 1" in metrics_response.text
        assert "mi_events_total" in metrics_response.text
        assert "mi_candidate_rejections_total 3" in metrics_response.text
        assert (
            'mi_candidate_rejections_reason_total{reason="first_step_not_executable"} 2'
            in metrics_response.text
        )
        assert (
            'mi_candidate_rejections_reason_total{reason="invalid_parameters"} 1'
            in metrics_response.text
        )

        decisions_response = client.get("/decisions")
        assert decisions_response.status_code == 200
        decisions = decisions_response.json()
        assert len(decisions) == 1
        assert decisions[0]["incident_id"] == "inc-1"

        eval_runs_response = client.get("/eval/runs")
        assert eval_runs_response.status_code == 200
        eval_runs = eval_runs_response.json()
        assert len(eval_runs) == 1
        assert eval_runs[0]["run_id"] == "run-1"



@pytest.mark.asyncio()
async def test_events_stream_emits_versioned_sse_schema(test_settings) -> None:
    agent, context = build_agent(test_settings, FakeLLMClient())
    app = create_api(context, agent)

    async def _next_event() -> Event:
        return Event(event_type="test.event", payload={"ok": True})

    context.events.next_event = _next_event  # type: ignore[method-assign]
    stream_route = next(
        route
        for route in app.routes
        if getattr(route, "path", None) == "/events/stream"
    )
    response = await stream_route.endpoint()
    first_chunk = await anext(response.body_iterator)

    line = first_chunk.decode() if isinstance(first_chunk, bytes) else str(first_chunk)
    assert line.startswith("data: ")
    payload = json.loads(line.removeprefix("data: ").strip())
    assert payload["schema_version"] == "v1"
    assert payload["event"]["event_type"] == "test.event"


@pytest.mark.asyncio()
async def test_recovery_emits_otel_spans_per_loop_phase(test_settings) -> None:
    llm = FakeLLMClient()
    llm.queue_response(
        "diagnose",
        {
            "failure_type": "db_file_missing",
            "hypotheses": ["database file missing"],
            "evidence": ["log signature matched"],
        },
    )
    llm.queue_response(
        "propose_plans",
        [
            {
                "name": "recover",
                "rationale": "apply schema",
                "steps": [
                    {
                        "action_id": "apply_schema",
                        "parameters": {"db_path": "data/app.db", "schema_path": "schema.sql"},
                        "preconditions": ["probe:db_file.exists=false"],
                        "expected_postconditions": ["probe:schema_ok=true"],
                    }
                ],
            }
        ],
    )
    llm.queue_response(
        "distill_skill",
        {
            "name": "recover-db",
            "description": "Apply schema",
            "failure_type": "db_file_missing",
            "trigger_fingerprint": {},
            "steps": [
                {
                    "action_id": "apply_schema",
                    "parameters": {"db_path": "data/app.db", "schema_path": "schema.sql"},
                }
            ],
            "expected_postcondition": "healthy",
        },
    )

    agent, context = build_agent(test_settings, llm)
    context.verifier.health_endpoint_ok = lambda: _async_true()  # type: ignore[method-assign]

    test_settings.db_file.unlink()
    test_settings.app_log.write_text(
        "sqlite3.OperationalError: unable to open database file\n",
        encoding="utf-8",
    )
    await agent._recover_loop()

    span_events = [event for event in context.events.events if event.event_type == "otel.span"]
    assert span_events
    span_names = {str(event.payload.get("name")) for event in span_events}
    assert "loop.observe" in span_names
    assert "loop.execute" in span_names
    assert "loop.verify" in span_names
    assert all(event.payload.get("schema_version") == "v1" for event in span_events)
