from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, HTMLResponse

from mi_agent.agent.engine import RecoveryAgent
from mi_agent.observability import sanitize_payload
from mi_agent.runtime import MIContext


def _event_incident_id(event_row: dict[str, Any]) -> str | None:
    payload = event_row.get("payload")
    if not isinstance(payload, dict):
        return None
    incident_id = payload.get("incident_id")
    if isinstance(incident_id, str) and incident_id != "":
        return incident_id
    return None


def _is_healthy_monitor_event(event_row: dict[str, Any]) -> bool:
    if str(event_row.get("event_type")) != "monitor.health":
        return False
    payload = event_row.get("payload")
    if not isinstance(payload, dict):
        return False
    return bool(payload.get("healthy", False))


def _build_history(events: list[dict[str, Any]], *, limit: int = 50) -> list[dict[str, Any]]:
    by_incident: dict[str, dict[str, Any]] = {}

    def _record(incident_id: str) -> dict[str, Any]:
        return by_incident.setdefault(
            incident_id,
            {
                "incident_id": incident_id,
                "status": "in_progress",
                "started_ts": None,
                "recovered_ts": None,
                "closed_ts": None,
                "attempts": 0,
                "failure_type": None,
                "final_tool_name": None,
                "executed_candidate": None,
                "last_error": None,
            },
        )

    for row in events:
        incident_id = _event_incident_id(row)
        if incident_id is None:
            continue
        event_type = str(row.get("event_type") or "")
        payload = row.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        ts = row.get("ts")
        ts_value = float(ts) if isinstance(ts, (int, float)) else None

        record = _record(incident_id)
        if event_type == "incident.started" and ts_value is not None:
            record["started_ts"] = ts_value
        elif event_type == "diagnosis.why":
            failure_type = payload.get("failure_type")
            if isinstance(failure_type, str):
                record["failure_type"] = failure_type
        elif event_type == "planning.how":
            executed_candidate = payload.get("executed_candidate")
            if isinstance(executed_candidate, str):
                record["executed_candidate"] = executed_candidate
        elif event_type == "incident.recovery_failed":
            attempts = payload.get("attempts")
            if isinstance(attempts, int):
                record["attempts"] = attempts
            error = payload.get("error")
            if isinstance(error, str):
                record["last_error"] = error
        elif event_type == "incident.recovered":
            record["status"] = "recovered"
            if ts_value is not None:
                record["recovered_ts"] = ts_value
            attempts = payload.get("attempts")
            if isinstance(attempts, int):
                record["attempts"] = attempts
            tool_name = payload.get("tool_name")
            if isinstance(tool_name, str):
                record["final_tool_name"] = tool_name
        elif event_type == "incident.closed":
            if ts_value is not None:
                record["closed_ts"] = ts_value
            attempts = payload.get("attempts")
            if isinstance(attempts, int):
                record["attempts"] = attempts

    recovered_rows = [row for row in by_incident.values() if row.get("status") == "recovered"]
    recovered_rows.sort(
        key=lambda row: float(row.get("recovered_ts") or row.get("started_ts") or 0.0),
        reverse=True,
    )
    return recovered_rows[: max(1, limit)]


def create_api(context: MIContext, agent: RecoveryAgent) -> FastAPI:
    app = FastAPI(title="MI Recovery Agent", version="2.0")
    loop_task: asyncio.Task[None] | None = None
    dashboard_file = Path.cwd() / "dashboard" / "index.html"

    @app.on_event("startup")
    async def _startup() -> None:
        nonlocal loop_task
        preflight = await context.llm.preflight()
        await context.events.publish("llm.preflight", preflight)
        if loop_task is None or loop_task.done():
            loop_task = asyncio.create_task(agent.run_forever())

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        nonlocal loop_task
        if loop_task is not None:
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task

    @app.get("/health/live")
    async def health_live() -> dict[str, Any]:
        return {"ok": True, "service": "mi-agent"}

    @app.get("/dashboard")
    async def dashboard_html() -> Any:
        if dashboard_file.exists():
            return FileResponse(dashboard_file)
        return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)

    @app.get("/agent/status")
    async def agent_status() -> dict[str, Any]:
        snapshot = agent.snapshot
        incident = snapshot.incident
        incident_payload = None
        if incident is not None:
            incident_payload = {
                "incident_id": incident.incident_id,
                "attempts": incident.attempts,
                "last_tool_error": incident.last_tool_error,
                "failure_type": incident.diagnosis.failure_type if incident.diagnosis else None,
            }
        return {
            "workflow_state": snapshot.workflow_state.value,
            "incident": incident_payload,
            "last_monitor": snapshot.last_monitor.model_dump() if snapshot.last_monitor else None,
        }

    @app.get("/agent/dashboard")
    async def agent_dashboard(limit: int = 200) -> dict[str, Any]:
        safe_limit = max(1, min(limit, 1000))
        scan_limit = max(500, min(4000, safe_limit * 8))
        snapshot = agent.snapshot
        incident = snapshot.incident
        active_incident_id: str | None = None
        incident_payload: dict[str, Any] | None = None
        if incident is not None and snapshot.workflow_state.value == "incident_active":
            active_incident_id = incident.incident_id
            incident_payload = {
                "incident_id": incident.incident_id,
                "attempts": incident.attempts,
                "last_error": incident.last_tool_error,
                "failure_type": incident.diagnosis.failure_type if incident.diagnosis else None,
            }

        all_events = context.events.tail(limit=scan_limit)
        if active_incident_id is None:
            timeline: list[dict[str, Any]] = []
        else:
            timeline_rows = [
                row
                for row in all_events
                if (
                    (_event_incident_id(row) == active_incident_id)
                    or (
                        str(row.get("event_type") or "") == "monitor.health"
                        and not _is_healthy_monitor_event(row)
                    )
                )
            ]
            timeline = list(reversed(timeline_rows[-safe_limit:]))

        history = _build_history(all_events, limit=50)

        policy = agent.policy
        rl_actions: list[dict[str, Any]] = []
        for action, values in policy.items():
            attempts = float(values.get("attempts", 0.0))
            reward_total = float(values.get("reward_total", 0.0))
            avg_reward = reward_total / attempts if attempts > 0 else 0.0
            rl_actions.append(
                {
                    "action": action,
                    "attempts": attempts,
                    "reward_total": reward_total,
                    "avg_reward": avg_reward,
                }
            )
        rl_actions.sort(key=lambda row: (row["reward_total"], row["attempts"]), reverse=True)

        trace = agent.get_llm_trace(active_incident_id) if active_incident_id else None
        latest_why = trace.get("why") if isinstance(trace, dict) else None
        latest_how = trace.get("how") if isinstance(trace, dict) else None

        return {
            "agent_state": {
                "workflow_state": snapshot.workflow_state.value,
                "active_incident": incident_payload,
                "last_monitor": (
                    snapshot.last_monitor.model_dump() if snapshot.last_monitor else None
                ),
            },
            "timeline": sanitize_payload(timeline, max_items=200, max_string_length=600),
            "history": sanitize_payload(history, max_items=100, max_string_length=600),
            "rl_summary": {
                "actions": rl_actions,
                "total_attempts": sum(row["attempts"] for row in rl_actions),
                "reward_total": sum(row["reward_total"] for row in rl_actions),
            },
            "tool_catalog": context.tools.catalog,
            "latest_why": latest_why,
            "latest_how": latest_how,
            "latest_trace_incident_id": active_incident_id,
        }

    @app.get("/agent/llm-trace")
    async def llm_trace(incident_id: str | None = Query(default=None)) -> dict[str, Any]:
        trace = agent.get_llm_trace(incident_id)
        return {
            "trace": (
                sanitize_payload(trace, max_items=80, max_string_length=1000)
                if isinstance(trace, dict)
                else None
            ),
            "available_incident_ids": agent.recent_llm_trace_ids(limit=50),
        }

    @app.get("/agent/events")
    async def agent_events(limit: int = 200) -> dict[str, Any]:
        safe_limit = max(1, min(2000, limit))
        return {"events": context.events.tail(limit=safe_limit)}

    @app.get("/agent/tool-catalog")
    async def tool_catalog() -> dict[str, Any]:
        return {"catalog": context.tools.catalog}

    @app.get("/agent/policy")
    async def policy() -> dict[str, Any]:
        return {"policy": agent.policy}

    return app
