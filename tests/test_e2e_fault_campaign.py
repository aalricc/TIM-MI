from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REQUEST_BACKUP_RESTORE = "request_backup_restore"
RUN_SQL = "run_sql"


def _write_fixture(path: Path, *, scenario_id: str, repetitions: int = 1) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "scenario_id": scenario_id,
                "fault": "drop_widgets_table",
                "repetitions": repetitions,
                "approved": {
                    "operations": [RUN_SQL, REQUEST_BACKUP_RESTORE],
                    "max_attempts": 3,
                    "max_ttr_seconds": 300,
                },
                "source": "docs/runbooks/drop_widgets_table.md",
            }
        ),
        encoding="utf-8",
    )


def _load_script() -> ModuleType:
    script_path = Path("scripts/e2e_fault_campaign.py")
    spec = importlib.util.spec_from_file_location("e2e_fault_campaign", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_incident_window_picks_new_incident_after_cutoff() -> None:
    script = _load_script()
    events = [
        {
            "ts": 1.0,
            "event_type": "incident.started",
            "payload": {"incident_id": "inc-old"},
        },
        {
            "ts": 2.0,
            "event_type": "incident.closed",
            "payload": {"incident_id": "inc-old"},
        },
        {
            "ts": 10.0,
            "event_type": "incident.started",
            "payload": {"incident_id": "inc-new"},
        },
        {
            "ts": 15.0,
            "event_type": "incident.closed",
            "payload": {"incident_id": "inc-new"},
        },
    ]

    assert script._incident_window(events, since_ts=5.0) == ("inc-new", 10.0, 15.0)


def test_wait_for_incident_closed_uses_agent_events(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _load_script()
    polls = [
        [
            {
                "ts": 10.0,
                "event_type": "incident.started",
                "payload": {"incident_id": "inc-1"},
            }
        ],
        [
            {
                "ts": 10.0,
                "event_type": "incident.started",
                "payload": {"incident_id": "inc-1"},
            },
            {
                "ts": 12.0,
                "event_type": "incident.closed",
                "payload": {"incident_id": "inc-1"},
            },
        ],
    ]

    def _fake_events(_client: object) -> list[dict[str, object]]:
        return polls.pop(0)

    monkeypatch.setattr(script, "_agent_events", _fake_events)
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)

    incident_id, started_ts, closed_ts = script._wait_for_incident_closed(
        object(),
        since_ts=5.0,
        timeout_seconds=5.0,
        poll_seconds=0.0,
    )
    assert (incident_id, started_ts, closed_ts) == ("inc-1", 10.0, 12.0)


def test_wait_for_incident_closed_handles_transient_event_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_script()
    polls: list[list[dict[str, object]]] = [
        [
            {
                "ts": 10.0,
                "event_type": "incident.started",
                "payload": {"incident_id": "inc-2"},
            },
            {
                "ts": 14.0,
                "event_type": "incident.closed",
                "payload": {"incident_id": "inc-2"},
            },
        ]
    ]

    calls = {"n": 0}

    def _fake_events(_client: object) -> list[dict[str, object]]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("temporary /agent/events error")
        return polls.pop(0)

    monkeypatch.setattr(script, "_agent_events", _fake_events)
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)

    incident_id, started_ts, closed_ts = script._wait_for_incident_closed(
        object(),
        since_ts=5.0,
        timeout_seconds=5.0,
        poll_seconds=0.0,
    )
    assert (incident_id, started_ts, closed_ts) == ("inc-2", 10.0, 14.0)


def test_wait_for_incident_closed_timeout_without_start(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _load_script()
    monkeypatch.setattr(script, "_agent_events", lambda _client: [])
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="no incident started"):
        script._wait_for_incident_closed(
            object(),
            since_ts=5.0,
            timeout_seconds=0.01,
            poll_seconds=0.0,
        )


def test_wait_for_incident_closed_timeout_without_close(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _load_script()
    started = [
        {
            "ts": 10.0,
            "event_type": "incident.started",
            "payload": {"incident_id": "inc-3"},
        }
    ]
    monkeypatch.setattr(script, "_agent_events", lambda _client: started)
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="did not close"):
        script._wait_for_incident_closed(
            object(),
            since_ts=5.0,
            timeout_seconds=0.01,
            poll_seconds=0.0,
        )


def test_wait_for_target_healthy_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _load_script()
    monkeypatch.setattr(script, "_request_json", lambda *_args, **_kwargs: {"ok": False})
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="did not become healthy"):
        script._wait_for_target_healthy(object(), timeout_seconds=0.01, poll_seconds=0.0)


def test_slice_incident_retries_then_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_script()
    rows = [{"ts": 1.0, "event_type": "incident.started", "payload": {"incident_id": "inc-1"}}]
    calls = {"n": 0}

    def _fake_load(_path: Path) -> list[dict[str, object]]:
        return rows

    def _fake_slice(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        calls["n"] += 1
        if calls["n"] < 3:
            return []
        return rows

    monkeypatch.setattr(script, "load_event_log", _fake_load)
    monkeypatch.setattr(script, "slice_events", _fake_slice)
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)

    sliced = script._slice_incident(
        events_log=tmp_path / "events.jsonl",
        incident_id="inc-1",
        started_ts=1.0,
        closed_ts=2.0,
        retries=5,
        poll_seconds=0.0,
    )
    assert len(sliced) == 1
    assert calls["n"] == 3


def test_slice_incident_raises_after_retry_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_script()
    monkeypatch.setattr(script, "load_event_log", lambda _path: [])
    monkeypatch.setattr(script, "slice_events", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="not found"):
        script._slice_incident(
            events_log=tmp_path / "events.jsonl",
            incident_id="inc-1",
            started_ts=1.0,
            closed_ts=2.0,
            retries=2,
            poll_seconds=0.0,
        )


def test_resolve_fixtures_rejects_unknown_name(tmp_path: Path) -> None:
    script = _load_script()
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    _write_fixture(fixtures / "drop_widgets_table.json", scenario_id="drop_widgets_table")

    with pytest.raises(ValueError, match="unknown fixture"):
        script._resolve_fixtures(fixtures, ["missing"])


def test_main_writes_campaign_report_with_stubbed_scenarios(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_script()

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    _write_fixture(fixtures / "drop_widgets_table.json", scenario_id="drop_widgets_table")

    captured: dict[str, Any] = {}

    def _fake_run_scenario(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        run_dir = kwargs["run_dir"]
        assert isinstance(run_dir, Path)
        (run_dir / "drop_widgets_table.slice.jsonl").write_text("", encoding="utf-8")
        (run_dir / "drop_widgets_table.check.json").write_text("{}\n", encoding="utf-8")
        (run_dir / "drop_widgets_table.kpis.json").write_text("{}\n", encoding="utf-8")
        return {
            "scenario_id": "drop_widgets_table",
            "repetition": 1,
            "fault": "drop_widgets_table",
            "fixture": "stub",
            "incident_id": "inc-1",
            "started_ts": 1.0,
            "closed_ts": 2.0,
            "status": "pass",
            "violations": 0,
            "slice": "stub.slice.jsonl",
            "check": "stub.check.json",
            "kpis": "stub.kpis.json",
        }

    monkeypatch.setattr(script, "_run_scenario", _fake_run_scenario)

    class _DummyClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> _DummyClient:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    monkeypatch.setattr(script.httpx, "Client", _DummyClient)

    results_root = tmp_path / "results"
    run_id = "fault-campaign-test"
    code = script.main(
        [
            "--fixtures-dir",
            str(fixtures),
            "--results-root",
            str(results_root),
            "--run-id",
            run_id,
            "--events-log",
            str(tmp_path / "events.jsonl"),
        ]
    )
    assert code == 0

    report = json.loads((results_root / run_id / "campaign.json").read_text(encoding="utf-8"))
    assert report["schema_version"] == "v1"
    assert report["run_id"] == run_id
    assert report["stage"] == "ci"
    assert isinstance(report["started_at"], str)
    assert isinstance(report["finished_at"], str)
    assert report["overall_status"] == "pass"
    row = report["scenarios"][0]
    assert row["scenario_id"] == "drop_widgets_table"
    assert row["repetition"] == 1
    assert row["status"] == "pass"
    assert row["violations"] == 0
    assert {"slice", "check", "kpis"} <= set(row)
    assert captured["stage"] == "ci"
    assert captured["events_log"] == tmp_path / "events.jsonl"


def test_main_returns_nonzero_when_campaign_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_script()

    class _DummyClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> _DummyClient:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    monkeypatch.setattr(script.httpx, "Client", _DummyClient)
    monkeypatch.setattr(
        script,
        "run_campaign",
        lambda **_kwargs: {
            "schema_version": "v1",
            "run_id": "rid",
            "stage": "ci",
            "started_at": "s",
            "finished_at": "f",
            "overall_status": "fail",
            "scenarios": [],
        },
    )

    code = script.main(["--run-id", "rid"])
    assert code == 1


def test_run_campaign_continues_after_scenario_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_script()
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    _write_fixture(fixtures / "a.json", scenario_id="a")
    _write_fixture(fixtures / "b.json", scenario_id="b")

    calls = {"n": 0}

    def _fake_run_scenario(**kwargs: object) -> dict[str, object]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return {
            "scenario_id": str(kwargs["fixture_path"].stem),
            "repetition": 1,
            "fault": "drop_widgets_table",
            "fixture": str(kwargs["fixture_path"]),
            "incident_id": "inc-ok",
            "started_ts": 1.0,
            "closed_ts": 2.0,
            "status": "pass",
            "violations": 0,
            "slice": "x.slice.jsonl",
            "check": "x.check.json",
            "kpis": "x.kpis.json",
        }

    monkeypatch.setattr(script, "_run_scenario", _fake_run_scenario)

    report = script.run_campaign(
        fixtures_dir=fixtures,
        selected_fixtures=[],
        results_root=tmp_path / "results",
        run_id="run1",
        stage="ci",
        events_log=tmp_path / "events.jsonl",
        target_client=object(),
        agent_client=object(),
        healthy_timeout_seconds=1.0,
        incident_timeout_seconds=1.0,
        poll_seconds=0.1,
    )
    assert calls["n"] == 2
    assert report["overall_status"] == "fail"
    assert len(report["scenarios"]) == 2
    assert report["scenarios"][0]["status"] == "fail"
    assert report["scenarios"][1]["status"] == "pass"


def test_run_campaign_respects_fixture_repetitions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_script()
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    _write_fixture(fixtures / "repeat.json", scenario_id="repeat", repetitions=2)

    seen: list[tuple[str, int]] = []

    def _fake_run_scenario(**kwargs: object) -> dict[str, object]:
        prefix = str(kwargs["artifact_prefix"])
        repetition = int(kwargs["repetition"])
        seen.append((prefix, repetition))
        return {
            "scenario_id": "repeat",
            "repetition": repetition,
            "fault": "drop_widgets_table",
            "fixture": str(kwargs["fixture_path"]),
            "incident_id": "inc-repeat",
            "started_ts": 1.0,
            "closed_ts": 2.0,
            "status": "pass",
            "violations": 0,
            "slice": f"{prefix}.slice.jsonl",
            "check": f"{prefix}.check.json",
            "kpis": f"{prefix}.kpis.json",
        }

    monkeypatch.setattr(script, "_run_scenario", _fake_run_scenario)

    report = script.run_campaign(
        fixtures_dir=fixtures,
        selected_fixtures=[],
        results_root=tmp_path / "results",
        run_id="run2",
        stage="ci",
        events_log=tmp_path / "events.jsonl",
        target_client=object(),
        agent_client=object(),
        healthy_timeout_seconds=1.0,
        incident_timeout_seconds=1.0,
        poll_seconds=0.1,
    )
    assert report["overall_status"] == "pass"
    assert len(report["scenarios"]) == 2
    assert seen == [("repeat.r1", 1), ("repeat.r2", 2)]


def test_run_scenario_uses_agent_baseline_timestamp_when_host_clock_ahead(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_script()
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    fixture: dict[str, object] = {
        "scenario_id": "drop_widgets_table",
        "fault": "drop_widgets_table",
    }

    seen: dict[str, float] = {}

    monkeypatch.setattr(
        script,
        "_wait_for_target_healthy",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        script,
        "_agent_events",
        lambda *_args, **_kwargs: [{"ts": 100.0, "event_type": "heartbeat", "payload": {}}],
    )
    monkeypatch.setattr(script.time, "time", lambda: 200.0)
    monkeypatch.setattr(script, "_request_json", lambda *_args, **_kwargs: {})

    def _fake_wait_for_incident_closed(
        *_args: object,
        **kwargs: object,
    ) -> tuple[str, float, float]:
        seen["since_ts"] = float(kwargs["since_ts"])
        return "inc-1", 101.0, 102.0

    monkeypatch.setattr(script, "_wait_for_incident_closed", _fake_wait_for_incident_closed)
    monkeypatch.setattr(
        script,
        "_slice_incident",
        lambda **_kwargs: [{"ts": 101.0, "event_type": "incident.started", "payload": {}}],
    )
    monkeypatch.setattr(script, "evaluate_fixture", lambda *_args, **_kwargs: {"status": "pass"})
    monkeypatch.setattr(script, "compute_kpis", lambda _rows: {"recovery_rate": 1.0})

    row = script._run_scenario(
        fixture=fixture,
        fixture_path=tmp_path / "drop_widgets_table.json",
        artifact_prefix="drop_widgets_table",
        repetition=1,
        run_dir=run_dir,
        stage="ci",
        events_log=tmp_path / "events.jsonl",
        target_client=object(),
        agent_client=object(),
        healthy_timeout_seconds=10.0,
        incident_timeout_seconds=10.0,
        poll_seconds=1.0,
    )

    assert seen["since_ts"] == 100.0
    assert row["status"] == "pass"


def test_parser_rejects_non_positive_poll_seconds() -> None:
    script = _load_script()
    with pytest.raises(SystemExit):
        script._parser().parse_args(["--poll-seconds", "0"])


def test_live_campaign_workflow_is_manual_dispatch_only() -> None:
    workflow = Path(".github/workflows/live-e2e-fault-campaign.yml").read_text(
        encoding="utf-8"
    )
    assert "workflow_dispatch:" in workflow
    assert "schedule:" not in workflow
    assert "push:" not in workflow
    assert "pull_request:" not in workflow
    assert "runs-on: self-hosted" in workflow
    assert "pytest -q -m live" in workflow
