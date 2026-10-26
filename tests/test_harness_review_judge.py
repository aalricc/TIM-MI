from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from harness.contracts import contract_errors
from harness.review import judge as judge_mod
from harness.review.calibrate import calibrate
from harness.review.calibrate import main as calibrate_main
from harness.review.judge import _build_sensor_result
from harness.review.judge import main as judge_main


def _packet(
    *,
    incident_id: str = "inc-1",
    selected: bool = True,
    evidence: str = "ok",
) -> dict[str, Any]:
    traces: list[dict[str, Any]]
    if selected:
        traces = [
            {
                "outcome": "selected",
                "selected_candidate": {"operation": "run_sql"},
            }
        ]
    else:
        traces = [{"outcome": "unusable", "selected_candidate": None}]
    return {
        "incident_id": incident_id,
        "incident_signature": {
            "schema_version": "v1",
            "incident_id": incident_id,
            "started_at": 10.0,
            "monitor_detail": {"error": evidence},
            "failure_type": "schema_invalid",
            "hypotheses": ["h1"],
            "evidence": [evidence],
            "class_id": None,
            "class_match": None,
        },
        "decision_traces": traces,
        "outcome": {
            "incident_id": incident_id,
            "status": "recovered",
            "rows_lost": 0,
            "reward": 1.0,
        },
    }


def _response_payload(text: str) -> dict[str, Any]:
    return {"output": [{"content": [{"type": "output_text", "text": text}]}]}


def _serve(
    monkeypatch: pytest.MonkeyPatch,
    payloads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    real_client = httpx.Client
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content.decode("utf-8")))
        body = payloads.pop(0)
        return httpx.Response(200, json=body, request=request)

    def client_factory(**kwargs: Any) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("harness.review.judge.httpx.Client", client_factory)
    return sent


def _write(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_judge_packet_invalid_json_then_retry_success(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _serve(
        monkeypatch,
        [
            _response_payload("not json"),
            _response_payload(
                json.dumps(
                    {
                        "diagnosis_correct": True,
                        "action_appropriate": True,
                        "notes": "all checks pass",
                        "failed_checks": [],
                        "rubric_version": "v1",
                    }
                )
            ),
        ],
    )

    verdict = judge_mod._judge_packet(
        _packet(evidence="drop_widgets_table"),
        model="gpt-5.3",
        endpoint="http://judge.invalid",
        api_key="k",
        rubric_text="rubric",
        approved_actions={"x": "approved"},
    )

    assert verdict["status"] == "verdict"
    assert verdict["diagnosis_correct"] is True
    assert len(sent) == 2


def test_judge_packet_skips_after_two_invalid_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, [_response_payload("bad"), _response_payload("still bad")])

    verdict = judge_mod._judge_packet(
        _packet(),
        model="gpt-5.3",
        endpoint="http://judge.invalid",
        api_key="k",
        rubric_text="rubric",
        approved_actions={"x": "approved"},
    )

    assert verdict["status"] == "skip"
    assert "invalid_model_output_after_retry" in str(verdict["skip_reason"])
    assert contract_errors(verdict, "judge_verdict") == []


def test_packet_without_selected_action_is_skipped_without_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fail_handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        calls.append({"url": str(request.url)})
        return httpx.Response(500, request=request)

    monkeypatch.setattr(
        "harness.review.judge.httpx.Client",
        lambda **kwargs: httpx.Client(transport=httpx.MockTransport(fail_handler), **kwargs),
    )

    verdict = judge_mod._judge_packet(
        _packet(selected=False),
        model="gpt-5.3",
        endpoint="http://judge.invalid",
        api_key="k",
        rubric_text="rubric",
        approved_actions={"x": "approved"},
    )

    assert verdict["status"] == "skip"
    assert verdict["skip_reason"] == "no_selected_action"
    assert calls == []


def test_fault_name_is_not_sent_to_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _serve(
        monkeypatch,
        [
            _response_payload(
                json.dumps(
                    {
                        "diagnosis_correct": True,
                        "action_appropriate": True,
                        "notes": "ok",
                        "failed_checks": [],
                        "rubric_version": "v1",
                    }
                )
            )
        ],
    )

    runbooks = []
    for name in ("drop_widgets_table.md", "delete_db.md", "corrupt_db.md"):
        path = tmp_path / name
        path.write_text(
            "# Runbook\n\n"
            "## Approved recovery actions\n"
            "- run_sql\n\n"
            "## Not approved\n"
            "- wipe db\n",
            encoding="utf-8",
        )
        runbooks.append(path)
    approved_actions = judge_mod._extract_approved_actions_sections(runbooks)
    assert list(approved_actions) == ["runbook_1", "runbook_2", "runbook_3"]

    verdict = judge_mod._judge_packet(
        _packet(evidence="fault injected: drop_widgets_table"),
        model="gpt-5.3",
        endpoint="http://judge.invalid",
        api_key="k",
        rubric_text="rubric",
        approved_actions=approved_actions,
    )

    assert verdict["status"] == "verdict"
    serialized = json.dumps(sent[0])
    assert "drop_widgets_table" not in serialized
    assert "delete_db" not in serialized
    assert "corrupt_db" not in serialized


def test_sensor_threshold_boundaries_pass_and_fail() -> None:
    rows_pass = [
        {
            "schema_version": "v1",
            "incident_id": f"inc-{i}",
            "diagnosis_correct": i < 4,
            "action_appropriate": i < 4,
            "notes": "n",
            "failed_checks": [],
            "rubric_version": "v1",
            "judge_model": "gpt-5.3",
            "status": "verdict",
        }
        for i in range(5)
    ]
    sensor_pass, code_pass = _build_sensor_result(
        rows_pass,
        trusted=True,
        stage="continuous",
        now=1.0,
    )
    assert (sensor_pass["status"], code_pass) == ("pass", 0)

    rows_fail = [
        *rows_pass[:-1],
        {
            **rows_pass[-1],
            "diagnosis_correct": False,
            "action_appropriate": False,
            "failed_checks": ["A1"],
        }
    ]
    sensor_fail, code_fail = _build_sensor_result(
        rows_fail,
        trusted=True,
        stage="continuous",
        now=1.0,
    )
    assert (sensor_fail["status"], code_fail) == ("fail", 1)


def test_untrusted_calibration_never_fails_even_with_bad_scores() -> None:
    rows = [
        {
            "schema_version": "v1",
            "incident_id": "inc-1",
            "diagnosis_correct": False,
            "action_appropriate": False,
            "notes": "n",
            "failed_checks": ["A1", "A2"],
            "rubric_version": "v1",
            "judge_model": "gpt-5.3",
            "status": "verdict",
        }
    ]

    sensor, code = _build_sensor_result(rows, trusted=False, stage="continuous", now=1.0)

    assert (sensor["status"], sensor["severity"], code) == ("pass", "info", 0)
    assert sensor["violations"] == []


def test_calibrate_boundaries_exactly_point8_and_20_packets() -> None:
    human = {
        f"inc-{i}": {
            "schema_version": "v1",
            "incident_id": f"inc-{i}",
            "diagnosis_correct": i < 20,
            "action_appropriate": i < 20,
            "notes": "",
        }
        for i in range(20)
    }
    judge = {
        f"inc-{i}": {
            "schema_version": "v1",
            "incident_id": f"inc-{i}",
            "diagnosis_correct": i < 16,
            "action_appropriate": i < 16,
            "notes": "",
            "failed_checks": [],
            "rubric_version": "v1",
            "judge_model": "gpt-5.3",
            "status": "verdict",
        }
        for i in range(20)
    }

    payload = calibrate(human, judge)

    assert payload["matched_packets"] == 20
    assert payload["agreement"]["diagnosis_correct"] == 0.8
    assert payload["agreement"]["action_appropriate"] == 0.8
    assert payload["trusted"] is True


def test_calibrate_untrusted_with_19_packets() -> None:
    human = {
        f"inc-{i}": {
            "schema_version": "v1",
            "incident_id": f"inc-{i}",
            "diagnosis_correct": True,
            "action_appropriate": True,
            "notes": "",
        }
        for i in range(19)
    }
    judge = {
        f"inc-{i}": {
            "schema_version": "v1",
            "incident_id": f"inc-{i}",
            "diagnosis_correct": True,
            "action_appropriate": True,
            "notes": "",
            "failed_checks": [],
            "rubric_version": "v1",
            "judge_model": "gpt-5.3",
            "status": "verdict",
        }
        for i in range(19)
    }

    payload = calibrate(human, judge)

    assert payload["matched_packets"] == 19
    assert payload["trusted"] is False


def test_calibrate_reports_disagreements() -> None:
    human = {
        "inc-1": {
            "schema_version": "v1",
            "incident_id": "inc-1",
            "diagnosis_correct": True,
            "action_appropriate": True,
            "notes": "",
        }
    }
    judge = {
        "inc-1": {
            "schema_version": "v1",
            "incident_id": "inc-1",
            "diagnosis_correct": False,
            "action_appropriate": True,
            "notes": "",
            "failed_checks": ["D1"],
            "rubric_version": "v1",
            "judge_model": "gpt-5.3",
            "status": "verdict",
        }
    }

    payload = calibrate(human, judge)

    assert payload["disagreements"] == [
        {
            "incident_id": "inc-1",
            "field": "diagnosis_correct",
            "human": True,
            "judge": False,
        }
    ]


def test_judge_main_missing_api_key_exits_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _write(tmp_path / "events.jsonl", [])

    assert judge_main(["prog", "--events", str(tmp_path / "events.jsonl")]) == 2


def test_judge_main_langsmith_flag_requires_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _write(tmp_path / "events.jsonl", [])
    monkeypatch.setattr(
        "harness.review.judge._load_langsmith_client_class",
        lambda: (_ for _ in ()).throw(RuntimeError("langsmith is not installed")),
    )

    assert judge_main(["prog", "--events", str(tmp_path / "events.jsonl"), "--langsmith"]) == 2


def test_calibrate_cli_prints_json(tmp_path: Path) -> None:
    reviews = tmp_path / "reviews"
    reviews.mkdir()
    _write(
        reviews / "inc-1.json",
        {
            "schema_version": "v1",
            "incident_id": "inc-1",
            "diagnosis_correct": True,
            "action_appropriate": False,
            "notes": "x",
        },
    )
    judge_file = tmp_path / "judge.jsonl"
    judge_file.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "incident_id": "inc-1",
                "diagnosis_correct": True,
                "action_appropriate": False,
                "notes": "x",
                "failed_checks": [],
                "rubric_version": "v1",
                "judge_model": "gpt-5.3",
                "status": "verdict",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert calibrate_main(["prog", str(reviews), str(judge_file)]) == 0


def test_judge_main_outputs_contract_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _serve(
        monkeypatch,
        [
            _response_payload(
                json.dumps(
                    {
                        "diagnosis_correct": True,
                        "action_appropriate": True,
                        "notes": "ok",
                        "failed_checks": [],
                        "rubric_version": "v1",
                    }
                )
            )
        ],
    )
    events = tmp_path / "events.jsonl"
    events.write_text("", encoding="utf-8")
    calibration = _write(tmp_path / "calibration.json", {"trusted": True})

    monkeypatch.setattr("harness.review.judge.sample_packets", lambda _events, n: [_packet()])
    monkeypatch.setattr("harness.review.judge._read_rubric", lambda: "rubric")
    monkeypatch.setattr(
        "harness.review.judge._extract_approved_actions_sections",
        lambda: {"drop_widgets_table.md": "approved"},
    )

    assert (
        judge_main(
            ["prog", "--events", str(events), "--calibration", str(calibration), "--n", "1"]
        )
        == 0
    )
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert contract_errors(lines[0], "judge_verdict") == []
    assert contract_errors(lines[1], "sensor_result") == []


def test_langsmith_mirror_calls_dataset_and_experiment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    langsmith = pytest.importorskip("langsmith")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("LANGSMITH_PROJECT", "mi-review-judge")

    sent = _serve(
        monkeypatch,
        [
            _response_payload(
                json.dumps(
                    {
                        "diagnosis_correct": True,
                        "action_appropriate": True,
                        "notes": "ok",
                        "failed_checks": [],
                        "rubric_version": "v1",
                    }
                )
            )
        ],
    )

    events = tmp_path / "events.jsonl"
    events.write_text("", encoding="utf-8")
    calibration = _write(tmp_path / "calibration.json", {"trusted": False})

    monkeypatch.setattr("harness.review.judge.sample_packets", lambda _events, n: [_packet()])
    monkeypatch.setattr("harness.review.judge._read_rubric", lambda: "rubric")
    monkeypatch.setattr(
        "harness.review.judge._extract_approved_actions_sections",
        lambda: {"drop_widgets_table.md": "approved"},
    )

    client_mock = MagicMock(spec=langsmith.Client)
    client_mock.create_dataset.return_value = {"id": "dataset-1"}

    class FakeClient:
        def __new__(cls) -> MagicMock:
            return client_mock

    monkeypatch.setattr("harness.review.judge._load_langsmith_client_class", lambda: FakeClient)

    assert (
        judge_main(
            [
                "prog",
                "--events",
                str(events),
                "--langsmith",
                "--calibration",
                str(calibration),
            ]
        )
        == 0
    )
    assert sent
    assert client_mock.create_dataset.called
    assert client_mock.create_example.called
    assert client_mock.create_run.called
