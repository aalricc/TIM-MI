from __future__ import annotations

import asyncio
import json
from pathlib import Path
from time import time
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse

from mi_agent.agent.agent import MIAgent, MIContext
from mi_agent.eval.reporting import build_dashboard_views


def create_api(context: MIContext, agent: MIAgent) -> FastAPI:
    app = FastAPI(title="MI API", version="0.1.0")

    def _candidate_rejection_reason_counts() -> dict[str, int]:
        counts: dict[str, int] = {}
        for event in context.events.events:
            if event.event_type != "agent.candidates_rejected_summary":
                continue
            payload = event.payload if isinstance(event.payload, dict) else {}
            reason_counts = payload.get("reason_counts")
            if not isinstance(reason_counts, dict):
                continue
            for reason, value in reason_counts.items():
                if not isinstance(reason, str) or not isinstance(value, int):
                    continue
                counts[reason] = counts.get(reason, 0) + value
        return counts

    def _read_eval_runs() -> list[dict[str, Any]]:
        eval_dir = context.settings.state_dir / "eval_runs"
        if not eval_dir.exists():
            return []

        runs: list[dict[str, Any]] = []
        for path in sorted(eval_dir.glob("*.json")):
            payload: dict[str, Any]
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {"error": "invalid_json"}
            run_id = payload.get("run_id") if isinstance(payload, dict) else None
            runs.append(
                {
                    "run_id": str(run_id) if run_id is not None else path.stem,
                    "path": path.as_posix(),
                    "summary": payload,
                }
            )
        return runs

    @app.on_event("startup")
    async def _on_startup() -> None:
        await agent.start()

    @app.on_event("shutdown")
    async def _on_shutdown() -> None:
        await agent.stop()

    @app.get("/state")
    async def state() -> dict[str, Any]:
        lock = context.supervisor.lock
        policy_snapshot = context.policy.snapshot()
        visits = sum(
            action.visits
            for failure in policy_snapshot.failures.values()
            for action in failure.actions.values()
        )
        has_skill = bool(context.skill_library.all())
        current_epsilon = context.selector.epsilon(visits=visits, has_proven_skill=has_skill)

        incident_payload: dict[str, Any] | None
        if lock is None:
            incident_payload = None
        else:
            elapsed = max(0.0, time() - lock.started_at.timestamp())
            incident_payload = {
                "incident_id": lock.incident_id,
                "failure_type": lock.failure_type,
                "attempts": lock.attempts,
                "elapsed_seconds": elapsed,
                "cap_attempts": context.settings.max_attempts_per_incident,
                "cap_seconds": context.settings.max_incident_seconds,
            }

        return {
            "incident": incident_payload,
            "epsilon": current_epsilon,
            "llm": {
                "model": context.settings.llm_model,
                "endpoint": context.settings.llm_endpoint,
                "auth_mode": context.settings.llm_auth_mode,
                "api_key_configured": bool(
                    context.settings.openai_api_key
                    and context.settings.openai_api_key.strip() != ""
                ),
            },
            "events_count": len(context.events.events),
        }

    @app.get("/policy")
    async def policy() -> dict[str, Any]:
        return context.policy.snapshot().model_dump(mode="json")

    @app.get("/skills")
    async def skills() -> list[dict[str, Any]]:
        return [
            skill.model_dump(mode="json")
            for skill in context.skill_library.all(include_inactive=True)
        ]

    @app.get("/incidents")
    async def incidents() -> list[dict[str, Any]]:
        incidents: list[dict[str, Any]] = []
        for event in context.events.events:
            if event.event_type in {
                "supervisor.incident_opened",
                "supervisor.incident_released",
                "supervisor.last_resort_restored",
                "skills.learned",
            }:
                incidents.append(event.model_dump(mode="json"))
        return incidents

    @app.get("/decisions")
    async def decisions() -> list[dict[str, Any]]:
        return [entry.model_dump(mode="json") for entry in context.decision_log.read_all()]

    @app.get("/eval/runs")
    async def eval_runs() -> list[dict[str, Any]]:
        return _read_eval_runs()

    @app.get("/dashboard/views")
    async def dashboard_views() -> dict[str, Any]:
        eval_runs_payload = _read_eval_runs()
        latest_summary: dict[str, Any] | None = None
        if eval_runs_payload:
            summary = eval_runs_payload[-1].get("summary")
            if isinstance(summary, dict):
                latest_summary = summary

        return build_dashboard_views(
            latest_eval_summary=latest_summary,
            decision_entries=context.decision_log.read_all(),
            skills=context.skill_library.all(include_inactive=True),
        )

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics() -> str:
        usage = getattr(context.llm, "usage", None)
        if usage is not None:
            llm_calls = int(getattr(usage, "calls", 0))
            llm_failures = int(getattr(usage, "failures", 0))
            llm_cost = float(getattr(usage, "estimated_cost_usd", 0.0))
        else:
            llm_calls = 0
            llm_failures = 0
            llm_cost = 0.0

        decision_count = len(context.decision_log.read_all())
        skill_count = len(context.skill_library.all(include_inactive=True))
        event_count = len(context.events.events)
        rejection_counts = _candidate_rejection_reason_counts()
        rejection_total = sum(rejection_counts.values())
        lines = [
            f"mi_events_total {event_count}",
            f"mi_decisions_total {decision_count}",
            f"mi_skills_total {skill_count}",
            f"mi_llm_calls_total {llm_calls}",
            f"mi_llm_failures_total {llm_failures}",
            f"mi_llm_estimated_cost_usd {llm_cost:.6f}",
            f"mi_candidate_rejections_total {rejection_total}",
        ]
        for reason in sorted(rejection_counts):
            escaped_reason = reason.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(
                f'mi_candidate_rejections_reason_total{{reason="{escaped_reason}"}} '
                f"{rejection_counts[reason]}"
            )
        return "\n".join(lines) + "\n"

    @app.get("/events/stream")
    async def event_stream() -> StreamingResponse:
        async def _generator() -> Any:
            while True:
                event = await context.events.next_event()
                payload = json.dumps(
                    {
                        "schema_version": "v1",
                        "event": event.model_dump(mode="json"),
                    }
                )
                yield f"data: {payload}\n\n"
                await asyncio.sleep(0)

        return StreamingResponse(_generator(), media_type="text/event-stream")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        dashboard_file = Path("dashboard/index.html")
        if dashboard_file.exists():
            return dashboard_file.read_text(encoding="utf-8")
        return DASHBOARD_HTML

    return app


DASHBOARD_HTML = """
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <title>MI Dashboard</title>
  <style>
    body { font-family: sans-serif; margin: 1rem; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
    pre { background: #111; color: #0f0; padding: 0.5rem; height: 280px; overflow: auto; }
    table, th, td { border: 1px solid #ccc; border-collapse: collapse; padding: 0.3rem; }
  </style>
</head>
<body>
  <h1>Maintenance Intelligence</h1>
  <div class=\"grid\">
    <div>
      <h3>Current State</h3>
      <pre id=\"state\"></pre>
      <h3>Policy Values</h3>
      <pre id=\"policy\"></pre>
    </div>
    <div>
      <h3>Skills</h3>
      <pre id=\"skills\"></pre>
      <h3>Events</h3>
      <pre id=\"events\"></pre>
    </div>
  </div>
<script>
async function refresh() {
  const [state, policy, skills] = await Promise.all([
    fetch('/state').then(r => r.json()),
    fetch('/policy').then(r => r.json()),
    fetch('/skills').then(r => r.json()),
  ]);
  document.getElementById('state').textContent = JSON.stringify(state, null, 2);
  document.getElementById('policy').textContent = JSON.stringify(policy, null, 2);
  document.getElementById('skills').textContent = JSON.stringify(skills, null, 2);
}
refresh();
setInterval(refresh, 2000);
const log = document.getElementById('events');
const es = new EventSource('/events/stream');
es.onmessage = (ev) => {
  const line = JSON.stringify(JSON.parse(ev.data));
  log.textContent = line + '\n' + log.textContent;
};
</script>
</body>
</html>
"""
