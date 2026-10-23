from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

REQUEST_BACKUP_RESTORE = "request_backup_restore"
RUN_SQL = "run_sql"


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


def test_main_writes_campaign_report_with_stubbed_scenarios(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_script()

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "drop_widgets_table.json").write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "scenario_id": "drop_widgets_table",
                "fault": "drop_widgets_table",
                "repetitions": 1,
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

    def _fake_run_scenario(**kwargs: object) -> dict[str, object]:
        run_dir = kwargs["run_dir"]
        assert isinstance(run_dir, Path)
        (run_dir / "drop_widgets_table.slice.jsonl").write_text("", encoding="utf-8")
        (run_dir / "drop_widgets_table.check.json").write_text("{}\n", encoding="utf-8")
        (run_dir / "drop_widgets_table.kpis.json").write_text("{}\n", encoding="utf-8")
        return {
            "scenario_id": "drop_widgets_table",
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
    assert report["overall_status"] == "pass"
    assert report["scenarios"][0]["scenario_id"] == "drop_widgets_table"
