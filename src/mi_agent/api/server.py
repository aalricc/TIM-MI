from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
from collections import defaultdict, deque
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


def _workflow_step_timeline(
    events: list[dict[str, Any]],
    *,
    incident_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    safe_limit = max(1, limit)
    rows = [
        row
        for row in events
        if str(row.get("event_type") or "") == "workflow.step"
        and _event_incident_id(row) == incident_id
    ]
    return list(reversed(rows[-safe_limit:]))


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


def _build_incident_views(
    events: list[dict[str, Any]],
    *,
    max_items: int = 6,
) -> dict[str, dict[str, Any]]:
    safe_max = max(1, max_items)
    rl_by_incident: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    tools_by_incident: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    pending_rl_keys: dict[str, deque[str]] = defaultdict(deque)

    for row in events:
        incident_id = _event_incident_id(row)
        if incident_id is None:
            continue
        event_type = str(row.get("event_type") or "")
        payload = row.get("payload")
        if not isinstance(payload, dict):
            payload = {}

        if event_type == "dynamic_tool.executed":
            action_key = payload.get("action_key")
            tool_name = payload.get("tool_name")
            operation = None
            operation_value = payload.get("operation")
            if isinstance(operation_value, str) and operation_value:
                operation = operation_value
            else:
                # Events logged before `operation` was a top-level field.
                action_details = payload.get("action")
                if isinstance(action_details, dict):
                    legacy_value = action_details.get("operation")
                    if isinstance(legacy_value, str) and legacy_value:
                        operation = legacy_value

            stat_key = (
                action_key
                if isinstance(action_key, str) and action_key
                else (
                    tool_name
                    if isinstance(tool_name, str) and tool_name
                    else "unknown_action"
                )
            )

            tool_row = tools_by_incident[incident_id].setdefault(
                stat_key,
                {
                    "incident_id": incident_id,
                    "action": stat_key,
                    "tool_name": stat_key,
                    "operation": "n/a",
                    "attempts": 0,
                },
            )
            tool_row["attempts"] = int(tool_row["attempts"]) + 1
            if isinstance(tool_name, str) and tool_name:
                tool_row["tool_name"] = tool_name
            if isinstance(operation, str) and operation:
                tool_row["operation"] = operation

            rl_row = rl_by_incident[incident_id].setdefault(
                stat_key,
                {
                    "incident_id": incident_id,
                    "action": stat_key,
                    "attempts": 0.0,
                    "reward_total": 0.0,
                    "avg_reward": 0.0,
                },
            )
            rl_row["attempts"] = float(rl_row["attempts"]) + 1.0
            pending_rl_keys[incident_id].append(stat_key)
            continue

        if event_type == "verification.result":
            queue = pending_rl_keys.get(incident_id)
            if not queue:
                continue
            stat_key = queue.popleft()
            rl_row = rl_by_incident[incident_id].setdefault(
                stat_key,
                {
                    "incident_id": incident_id,
                    "action": stat_key,
                    "attempts": 0.0,
                    "reward_total": 0.0,
                    "avg_reward": 0.0,
                },
            )
            reward = 1.0 if bool(payload.get("healthy", False)) else -1.0
            rl_row["reward_total"] = float(rl_row["reward_total"]) + reward

    incident_ids = sorted(set(rl_by_incident.keys()) | set(tools_by_incident.keys()))
    views: dict[str, dict[str, Any]] = {}
    for incident_id in incident_ids:
        rl_rows = list(rl_by_incident.get(incident_id, {}).values())
        for rl_row in rl_rows:
            attempts = float(rl_row.get("attempts", 0.0))
            reward_total = float(rl_row.get("reward_total", 0.0))
            rl_row["avg_reward"] = (reward_total / attempts) if attempts > 0 else 0.0
        rl_rows.sort(
            key=lambda row: (
                float(row.get("reward_total", 0.0)),
                float(row.get("attempts", 0.0)),
            ),
            reverse=True,
        )

        tool_rows = list(tools_by_incident.get(incident_id, {}).values())
        tool_rows.sort(
            key=lambda row: (
                int(row.get("attempts", 0)),
                str(row.get("action") or ""),
            ),
            reverse=True,
        )

        views[incident_id] = {
            "incident_id": incident_id,
            "rl_summary": {
                "actions": rl_rows[:safe_max],
                "total_attempts": sum(float(row.get("attempts", 0.0)) for row in rl_rows),
                "reward_total": sum(float(row.get("reward_total", 0.0)) for row in rl_rows),
            },
            "tool_catalog": tool_rows[:safe_max],
        }
    return views


def _build_history_incident_views(
    events: list[dict[str, Any]],
    *,
    timeline_limit: int = 30,
) -> dict[str, dict[str, Any]]:
    safe_limit = max(1, timeline_limit)
    by_incident: dict[str, dict[str, Any]] = {}

    def _record(incident_id: str) -> dict[str, Any]:
        return by_incident.setdefault(
            incident_id,
            {
                "incident_id": incident_id,
                "workflow_state": "incident_active",
                "attempts": 0,
                "last_error": None,
                "failure_type": None,
                "latest_why": None,
                "latest_how": None,
                "timeline_rows": [],
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

        record = _record(incident_id)
        record["timeline_rows"].append(row)

        if event_type == "diagnosis.why":
            record["latest_why"] = payload
            failure_type = payload.get("failure_type")
            if isinstance(failure_type, str):
                record["failure_type"] = failure_type
        elif event_type == "planning.how":
            record["latest_how"] = payload
        elif event_type == "incident.recovery_failed":
            attempts = payload.get("attempts")
            if isinstance(attempts, int):
                record["attempts"] = attempts
            error = payload.get("error")
            if isinstance(error, str):
                record["last_error"] = error
        elif event_type == "incident.recovered":
            attempts = payload.get("attempts")
            if isinstance(attempts, int):
                record["attempts"] = attempts
            record["workflow_state"] = "recovered"
        elif event_type == "incident.closed":
            record["workflow_state"] = "healthy"
            attempts = payload.get("attempts")
            if isinstance(attempts, int):
                record["attempts"] = attempts

    views: dict[str, dict[str, Any]] = {}
    for incident_id, record in by_incident.items():
        timeline_rows = list(record.get("timeline_rows") or [])
        workflow_rows = [
            row
            for row in timeline_rows
            if str(row.get("event_type") or "") == "workflow.step"
        ]
        timeline_source = workflow_rows if workflow_rows else timeline_rows
        timeline = list(reversed(timeline_source[-safe_limit:]))
        views[incident_id] = {
            "incident_id": incident_id,
            "agent_state": {
                "workflow_state": record.get("workflow_state", "incident_active"),
                "active_incident": {
                    "incident_id": incident_id,
                    "attempts": int(record.get("attempts") or 0),
                    "last_error": record.get("last_error"),
                    "failure_type": record.get("failure_type"),
                },
                "last_monitor": None,
            },
            "latest_why": record.get("latest_why"),
            "latest_how": record.get("latest_how"),
            "timeline": timeline,
        }
    return views


def latest_harness_results(path: Path, *, tail_lines: int = 200) -> dict[str, Any]:
    """The newest harness-watch run: the records that share the latest `ts`."""
    if not path.exists():
        return {"available": False, "ts": None, "status": None, "results": []}
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-tail_lines:]:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and isinstance(record.get("ts"), int | float):
            records.append(record)
    if not records:
        return {"available": False, "ts": None, "status": None, "results": []}
    latest_ts = max(float(record["ts"]) for record in records)
    latest = [record for record in records if float(record["ts"]) == latest_ts]
    failed = any(record.get("status") == "fail" for record in latest)
    return {
        "available": True,
        "ts": latest_ts,
        "status": "fail" if failed else "pass",
        "results": latest,
    }


def create_api(context: MIContext, agent: RecoveryAgent) -> FastAPI:
    app = FastAPI(title="MI Recovery Agent", version="2.0")
    loop_task: asyncio.Task[None] | None = None
    dashboard_file = Path.cwd() / "dashboard" / "index.html"

    @app.on_event("startup")
    async def _startup() -> None:
        nonlocal loop_task
        rag_summary: dict[str, Any] = {
            "enabled": context.settings.rag_enabled,
            "namespace": context.settings.pinecone_namespace,
            "top_k": context.settings.rag_top_k,
        }
        if context.settings.rag_enabled:
            try:
                pinecone = importlib.import_module("pinecone")
            except ModuleNotFoundError as exc:
                raise RuntimeError(
                    "RAG is enabled but Pinecone dependency is missing. "
                    "Install optional dependency group 'rag'."
                ) from exc
            rag_summary["pinecone_version"] = getattr(pinecone, "__version__", "unknown")
        await context.events.publish("rag.startup", rag_summary)

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
            workflow_timeline = _workflow_step_timeline(
                all_events,
                incident_id=active_incident_id,
                limit=safe_limit,
            )
            if workflow_timeline:
                timeline = workflow_timeline
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
        incident_views = _build_incident_views(all_events, max_items=6)
        history_incident_views = _build_history_incident_views(
            all_events,
            timeline_limit=min(200, safe_limit),
        )

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
            "incident_views": sanitize_payload(
                incident_views,
                max_items=200,
                max_string_length=600,
            ),
            "history_incident_views": sanitize_payload(
                history_incident_views,
                max_items=200,
                max_string_length=600,
            ),
            "live_incident_view": (
                sanitize_payload(
                    incident_views.get(active_incident_id, None),
                    max_items=50,
                    max_string_length=600,
                )
                if active_incident_id is not None
                else None
            ),
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

    @app.get("/agent/harness")
    async def harness_status() -> dict[str, Any]:
        return latest_harness_results(context.settings.harness_results_path)

    return app
