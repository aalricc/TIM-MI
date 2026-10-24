"""Run a live fault-injection campaign and write per-run results.

Fixtures in `harness/scenarios/*.json` run round-robin: every fixture once, then every
fixture again, up to its `repetitions` (or `--repetitions`). MI keeps its memory across
runs, so later repetitions show whether it learned (fast path). For each run:

1) target ready: wait for `GET /health/deep`; if it is still unhealthy (a previous run did
   not recover), `POST /ops/reset` and wait again; then wait until MI has no open incident
2) inject fault (`POST /ops/fault/{fault}`)
3) wait for `incident.closed` in MI events
4) slice the incident from `state/events.jsonl`
5) run approved-outcome checker
6) compute KPIs

The harness never touches the target while MI works on an incident. The target must run
with `TARGET_TEST_HOOKS=1` (fault and reset endpoints). Outputs are written to
`results/<run-id>/`, including KPIs over all runs (`campaign.kpis.json`).
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from harness.events.slice import slice_events
from harness.mappings.runtime import load_event_log
from harness.metrics.kpis import compute_kpis
from harness.scenarios.check import evaluate_fixture, load_fixture


def _event_ts(event: dict[str, Any]) -> float | None:
    value = event.get("ts")
    if isinstance(value, int | float) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number):
            return number
    return None


def _request_json(client: httpx.Client, method: str, path: str) -> dict[str, Any]:
    try:
        response = client.request(method, path)
        response.raise_for_status()
    except httpx.HTTPError as exc:  # pragma: no cover - exercised via caller behavior tests
        raise RuntimeError(f"{method} {path} failed: {exc}") from exc

    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{method} {path} did not return JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{method} {path} returned non-object JSON")
    return payload


def _agent_events(client: httpx.Client, *, limit: int = 2000) -> list[dict[str, Any]]:
    payload = _request_json(client, "GET", f"/agent/events?limit={limit}")
    rows = payload.get("events")
    if not isinstance(rows, list):
        raise RuntimeError("/agent/events payload missing 'events' list")
    return [dict(row) for row in rows if isinstance(row, dict)]


def _latest_event_ts(events: list[dict[str, Any]]) -> float | None:
    timestamps = [ts for event in events if (ts := _event_ts(event)) is not None]
    return max(timestamps) if timestamps else None


def _incident_window(
    events: list[dict[str, Any]],
    *,
    since_ts: float,
) -> tuple[str, float, float | None] | None:
    start_incident_id: str | None = None
    start_ts: float | None = None

    for event in events:
        if event.get("event_type") != "incident.started":
            continue
        ts = _event_ts(event)
        if ts is None or ts < since_ts:
            continue
        payload = event.get("payload")
        incident_id = payload.get("incident_id") if isinstance(payload, dict) else None
        if not isinstance(incident_id, str) or incident_id == "":
            continue
        if start_ts is None or ts < start_ts:
            start_incident_id = incident_id
            start_ts = ts

    if start_incident_id is None or start_ts is None:
        return None

    closed_ts: float | None = None
    for event in events:
        if event.get("event_type") != "incident.closed":
            continue
        ts = _event_ts(event)
        if ts is None or ts < start_ts:
            continue
        payload = event.get("payload")
        incident_id = payload.get("incident_id") if isinstance(payload, dict) else None
        if incident_id != start_incident_id:
            continue
        if closed_ts is None or ts < closed_ts:
            closed_ts = ts

    return start_incident_id, start_ts, closed_ts


def _wait_for_target_healthy(
    client: httpx.Client,
    *,
    timeout_seconds: float,
    poll_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            payload = _request_json(client, "GET", "/health/deep")
            if payload.get("ok") is True:
                return
        except RuntimeError:
            pass
        time.sleep(poll_seconds)
    raise RuntimeError(f"target did not become healthy within {timeout_seconds:.0f}s")


def _open_incidents(events: list[dict[str, Any]]) -> set[str]:
    started: set[str] = set()
    closed: set[str] = set()
    for event in events:
        payload = event.get("payload")
        incident_id = payload.get("incident_id") if isinstance(payload, dict) else None
        if not isinstance(incident_id, str):
            continue
        if event.get("event_type") == "incident.started":
            started.add(incident_id)
        elif event.get("event_type") == "incident.closed":
            closed.add(incident_id)
    return started - closed


def _wait_for_agent_idle(
    client: httpx.Client,
    *,
    timeout_seconds: float,
    poll_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    open_ids: set[str] = set()
    while time.monotonic() < deadline:
        try:
            open_ids = _open_incidents(_agent_events(client))
        except RuntimeError:
            time.sleep(poll_seconds)
            continue
        if not open_ids:
            return
        time.sleep(poll_seconds)
    raise RuntimeError(
        f"MI incident(s) {sorted(open_ids)} still open {timeout_seconds:.0f}s after the target "
        "was healthy"
    )


def _prepare_target(
    target_client: httpx.Client,
    agent_client: httpx.Client,
    *,
    ready_timeout_seconds: float,
    healthy_timeout_seconds: float,
    poll_seconds: float,
) -> bool:
    """Make the target healthy and MI idle before a run; True if the target was reset."""
    reset = False
    try:
        _wait_for_target_healthy(
            target_client,
            timeout_seconds=ready_timeout_seconds,
            poll_seconds=poll_seconds,
        )
    except RuntimeError:
        _request_json(target_client, "POST", "/ops/reset")
        reset = True
        _wait_for_target_healthy(
            target_client,
            timeout_seconds=healthy_timeout_seconds,
            poll_seconds=poll_seconds,
        )
    # A reset repairs the target from outside; MI's pre-retry health check then closes the
    # open incident without learning from it.
    _wait_for_agent_idle(
        agent_client,
        timeout_seconds=healthy_timeout_seconds,
        poll_seconds=poll_seconds,
    )
    return reset


def _recovery_outcome(rows: list[dict[str, Any]], incident_id: str) -> dict[str, Any]:
    """How the incident ended: recovered_via is fast_path, full_loop, or None."""
    started_ts: float | None = None
    for row in rows:
        payload = row.get("payload")
        if not isinstance(payload, dict) or payload.get("incident_id") != incident_id:
            continue
        if row.get("event_type") == "incident.started" and started_ts is None:
            started_ts = _event_ts(row)
        if row.get("event_type") == "incident.recovered":
            recovered_ts = _event_ts(row)
            attempts = payload.get("attempts")
            return {
                "recovered_via": (
                    "fast_path" if payload.get("source") == "class_memory" else "full_loop"
                ),
                "attempts": attempts if isinstance(attempts, int) else None,
                "ttr_seconds": (
                    round(recovered_ts - started_ts, 3)
                    if recovered_ts is not None and started_ts is not None
                    else None
                ),
            }
    return {"recovered_via": None, "attempts": None, "ttr_seconds": None}


def _wait_for_incident_closed(
    client: httpx.Client,
    *,
    since_ts: float,
    timeout_seconds: float,
    poll_seconds: float,
) -> tuple[str, float, float]:
    deadline = time.monotonic() + timeout_seconds
    last_incident: str | None = None

    while time.monotonic() < deadline:
        try:
            events = _agent_events(client)
        except RuntimeError:
            time.sleep(poll_seconds)
            continue
        window = _incident_window(events, since_ts=since_ts)
        if window is not None:
            incident_id, started_ts, closed_ts = window
            last_incident = incident_id
            if closed_ts is not None:
                return incident_id, started_ts, closed_ts
        time.sleep(poll_seconds)

    if last_incident is not None:
        raise RuntimeError(
            f"incident {last_incident!r} did not close within {timeout_seconds:.0f}s"
        )
    raise RuntimeError(f"no incident started within {timeout_seconds:.0f}s after fault injection")


def _slice_incident(
    *,
    events_log: Path,
    incident_id: str,
    started_ts: float,
    closed_ts: float,
    retries: int,
    poll_seconds: float,
) -> list[dict[str, Any]]:
    for _ in range(retries):
        events = load_event_log(events_log)
        rows = slice_events(events, since=started_ts, until=closed_ts, incident=incident_id)
        if rows:
            return rows
        time.sleep(poll_seconds)
    raise RuntimeError(f"incident {incident_id!r} not found in {events_log}")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":")))
            handle.write("\n")


def _run_scenario(
    *,
    fixture: Mapping[str, Any],
    fixture_path: Path,
    artifact_prefix: str,
    repetition: int,
    run_dir: Path,
    stage: str,
    events_log: Path,
    target_client: httpx.Client,
    agent_client: httpx.Client,
    healthy_timeout_seconds: float,
    incident_timeout_seconds: float,
    poll_seconds: float,
    ready_timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    scenario_id = str(fixture["scenario_id"])
    fault = str(fixture["fault"])

    effective_poll = poll_seconds if poll_seconds > 0 else 0.1
    slice_retries = max(5, math.ceil(incident_timeout_seconds / effective_poll))

    reset_before = _prepare_target(
        target_client,
        agent_client,
        ready_timeout_seconds=ready_timeout_seconds,
        healthy_timeout_seconds=healthy_timeout_seconds,
        poll_seconds=poll_seconds,
    )
    injected_at = time.time()
    since_ts = injected_at
    try:
        baseline_events = _agent_events(agent_client, limit=200)
    except RuntimeError:
        pass
    else:
        baseline_ts = _latest_event_ts(baseline_events)
        if baseline_ts is not None:
            since_ts = min(injected_at, baseline_ts)
    _request_json(target_client, "POST", f"/ops/fault/{fault}")

    incident_id, started_ts, closed_ts = _wait_for_incident_closed(
        agent_client,
        since_ts=since_ts,
        timeout_seconds=incident_timeout_seconds,
        poll_seconds=poll_seconds,
    )

    sliced = _slice_incident(
        events_log=events_log,
        incident_id=incident_id,
        started_ts=started_ts,
        closed_ts=closed_ts,
        retries=slice_retries,
        poll_seconds=poll_seconds,
    )

    slice_path = run_dir / f"{artifact_prefix}.slice.jsonl"
    check_path = run_dir / f"{artifact_prefix}.check.json"
    kpis_path = run_dir / f"{artifact_prefix}.kpis.json"

    _write_jsonl(slice_path, sliced)

    check_result = evaluate_fixture(fixture, sliced, stage=stage)
    _write_json(check_path, check_result)

    kpis = compute_kpis(sliced)
    _write_json(kpis_path, kpis)

    return {
        "scenario_id": scenario_id,
        "repetition": repetition,
        "fault": fault,
        "fixture": fixture_path.as_posix(),
        "incident_id": incident_id,
        "started_ts": started_ts,
        "closed_ts": closed_ts,
        "status": str(check_result["status"]),
        "violations": len(check_result.get("violations", [])),
        "reset_before": reset_before,
        **_recovery_outcome(sliced, incident_id),
        "slice": slice_path.as_posix(),
        "check": check_path.as_posix(),
        "kpis": kpis_path.as_posix(),
    }


def _resolve_fixtures(fixtures_dir: Path, selected: list[str]) -> list[Path]:
    all_fixtures = sorted(path for path in fixtures_dir.glob("*.json") if path.is_file())
    if not selected:
        return all_fixtures

    selected_names = set(selected)
    selected_paths = [
        path
        for path in all_fixtures
        if path.name in selected_names or path.stem in selected_names
    ]
    found = {path.name for path in selected_paths} | {path.stem for path in selected_paths}
    missing = [name for name in selected if name not in found]
    if missing:
        raise ValueError(f"unknown fixture(s): {', '.join(sorted(missing))}")
    return selected_paths


def _failure_row(
    run_dir: Path,
    *,
    scenario_id: str,
    artifact_prefix: str,
    repetition: int,
    fault: object,
    fixture_path: Path,
    error: str,
) -> dict[str, Any]:
    error_path = run_dir / f"{artifact_prefix}.error.txt"
    error_path.write_text(error + "\n", encoding="utf-8")
    return {
        "scenario_id": scenario_id,
        "repetition": repetition,
        "fault": None if fault is None else str(fault),
        "fixture": fixture_path.as_posix(),
        "incident_id": None,
        "status": "fail",
        "violations": 1,
        "error": error,
        "error_file": error_path.as_posix(),
    }


def _summary(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    by_fault: dict[str, dict[str, int]] = {}
    for row in rows:
        key = str(row.get("fault") or row.get("scenario_id"))
        entry = by_fault.setdefault(
            key, {"runs": 0, "passed": 0, "fast_path": 0, "full_loop": 0, "resets": 0}
        )
        entry["runs"] += 1
        entry["passed"] += 1 if row.get("status") == "pass" else 0
        if row.get("recovered_via") in {"fast_path", "full_loop"}:
            entry[str(row["recovered_via"])] += 1
        entry["resets"] += 1 if row.get("reset_before") else 0
    return dict(sorted(by_fault.items()))


def run_campaign(
    *,
    fixtures_dir: Path,
    selected_fixtures: list[str],
    results_root: Path,
    run_id: str,
    stage: str,
    events_log: Path,
    target_client: httpx.Client,
    agent_client: httpx.Client,
    healthy_timeout_seconds: float,
    incident_timeout_seconds: float,
    poll_seconds: float,
    repetitions_override: int | None = None,
    ready_timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    fixtures = _resolve_fixtures(fixtures_dir, selected_fixtures)
    if not fixtures:
        raise ValueError(f"no fixtures found in {fixtures_dir}")

    run_dir = results_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    started_at = datetime.now(tz=UTC).isoformat()

    rows: list[dict[str, Any]] = []
    loaded: list[tuple[Path, dict[str, Any], int]] = []

    for fixture_path in fixtures:
        scenario_id = fixture_path.stem
        try:
            fixture = load_fixture(fixture_path)
            repetitions = (
                repetitions_override
                if repetitions_override is not None
                else int(fixture.get("repetitions", 1))
            )
        except Exception as exc:
            rows.append(
                _failure_row(
                    run_dir,
                    scenario_id=scenario_id,
                    artifact_prefix=scenario_id,
                    repetition=1,
                    fault=None,
                    fixture_path=fixture_path,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        if repetitions < 1:
            rows.append(
                _failure_row(
                    run_dir,
                    scenario_id=scenario_id,
                    artifact_prefix=scenario_id,
                    repetition=1,
                    fault=fixture.get("fault"),
                    fixture_path=fixture_path,
                    error=f"invalid repetitions in fixture: {repetitions}",
                )
            )
            continue
        loaded.append((fixture_path, fixture, repetitions))

    # Round-robin: repetition r of every fixture runs before repetition r + 1 of any.
    max_repetitions = max((reps for _, _, reps in loaded), default=0)
    for repetition in range(1, max_repetitions + 1):
        for fixture_path, fixture, repetitions in loaded:
            if repetition > repetitions:
                continue
            scenario_id = fixture_path.stem
            artifact_prefix = scenario_id
            if repetitions > 1:
                artifact_prefix = f"{scenario_id}.r{repetition}"
            try:
                row = _run_scenario(
                    fixture=fixture,
                    fixture_path=fixture_path,
                    artifact_prefix=artifact_prefix,
                    repetition=repetition,
                    run_dir=run_dir,
                    stage=stage,
                    events_log=events_log,
                    target_client=target_client,
                    agent_client=agent_client,
                    healthy_timeout_seconds=healthy_timeout_seconds,
                    incident_timeout_seconds=incident_timeout_seconds,
                    poll_seconds=poll_seconds,
                    ready_timeout_seconds=ready_timeout_seconds,
                )
            except Exception as exc:
                rows.append(
                    _failure_row(
                        run_dir,
                        scenario_id=scenario_id,
                        artifact_prefix=artifact_prefix,
                        repetition=repetition,
                        fault=fixture.get("fault"),
                        fixture_path=fixture_path,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue
            rows.append(row)

    overall_status = "pass" if rows and all(row["status"] == "pass" for row in rows) else "fail"

    all_slices: list[dict[str, Any]] = []
    for row in rows:
        slice_file = row.get("slice")
        if isinstance(slice_file, str) and Path(slice_file).is_file():
            all_slices.extend(load_event_log(Path(slice_file)))
    campaign_kpis_path = run_dir / "campaign.kpis.json"
    _write_json(campaign_kpis_path, compute_kpis(all_slices))

    finished_at = datetime.now(tz=UTC).isoformat()
    report = {
        "schema_version": "v1",
        "run_id": run_id,
        "stage": stage,
        "started_at": started_at,
        "finished_at": finished_at,
        "overall_status": overall_status,
        "summary": _summary(rows),
        "kpis": campaign_kpis_path.as_posix(),
        "scenarios": rows,
    }
    _write_json(run_dir / "campaign.json", report)
    return report


def _parser() -> argparse.ArgumentParser:
    def _positive_int(value: str) -> int:
        number = int(value)
        if number < 1:
            raise argparse.ArgumentTypeError("must be >= 1")
        return number

    def _positive_float(value: str) -> float:
        number = float(value)
        if number <= 0:
            raise argparse.ArgumentTypeError("must be > 0")
        return number

    parser = argparse.ArgumentParser(description="Run live MI fault-injection campaign")
    parser.add_argument("--fixtures-dir", type=Path, default=Path("harness/scenarios"))
    parser.add_argument(
        "--fixture",
        action="append",
        default=[],
        help="Fixture filename or stem; pass multiple times to run a subset",
    )
    parser.add_argument("--results-root", type=Path, default=Path("results"))
    parser.add_argument("--run-id", help="Default: fault-campaign-YYYYMMDDTHHMMSSZ")
    parser.add_argument("--events-log", type=Path, default=Path("state/events.jsonl"))
    parser.add_argument("--target-url", default="http://127.0.0.1:8080")
    parser.add_argument("--agent-url", default="http://127.0.0.1:8090")
    parser.add_argument("--stage", choices=("ci", "continuous"), default="ci")
    parser.add_argument("--request-timeout-seconds", type=_positive_float, default=5.0)
    parser.add_argument("--healthy-timeout-seconds", type=_positive_float, default=90.0)
    parser.add_argument("--incident-timeout-seconds", type=_positive_float, default=420.0)
    parser.add_argument("--poll-seconds", type=_positive_float, default=1.0)
    parser.add_argument(
        "--ready-timeout-seconds",
        type=_positive_float,
        default=10.0,
        help="how long to wait for a healthy target before a run; then POST /ops/reset",
    )
    parser.add_argument(
        "--repetitions",
        type=_positive_int,
        help="runs per fixture (default: each fixture's `repetitions`)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    run_id = args.run_id or f"fault-campaign-{datetime.now(tz=UTC):%Y%m%dT%H%M%SZ}"

    with (
        httpx.Client(
            base_url=args.target_url,
            timeout=args.request_timeout_seconds,
        ) as target_client,
        httpx.Client(base_url=args.agent_url, timeout=args.request_timeout_seconds) as agent_client,
    ):
        report = run_campaign(
            fixtures_dir=args.fixtures_dir,
            selected_fixtures=list(args.fixture),
            results_root=args.results_root,
            run_id=run_id,
            stage=args.stage,
            events_log=args.events_log,
            target_client=target_client,
            agent_client=agent_client,
            healthy_timeout_seconds=args.healthy_timeout_seconds,
            incident_timeout_seconds=args.incident_timeout_seconds,
            poll_seconds=args.poll_seconds,
            repetitions_override=args.repetitions,
            ready_timeout_seconds=args.ready_timeout_seconds,
        )

    print(f"campaign report: {args.results_root / run_id / 'campaign.json'}")
    return 0 if report["overall_status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
