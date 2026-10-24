from __future__ import annotations

import importlib.util
import os
import re
import sqlite3
from pathlib import Path
from types import ModuleType

from fastapi.testclient import TestClient


def load_target_app_module(tmp_root: Path) -> ModuleType:
    os.environ["TARGET_APP_ROOT"] = tmp_root.as_posix()
    os.environ["TARGET_DB_PATH"] = (tmp_root / "data" / "app.db").as_posix()
    os.environ["TARGET_LOG_PATH"] = (tmp_root / "app.log").as_posix()
    os.environ["TARGET_SCHEMA_PATH"] = (tmp_root / "schema.sql").as_posix()
    os.environ["TARGET_HEARTBEAT_INTERVAL_SECONDS"] = "1"

    module_path = Path("target/app/main.py")
    spec = importlib.util.spec_from_file_location("target_app_main_test", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_target_app_generates_logs_and_exposes_health(tmp_path: Path) -> None:
    app_root = tmp_path / "target-app"
    (app_root / "data").mkdir(parents=True, exist_ok=True)
    (app_root / "schema.sql").write_text(
        "CREATE TABLE widgets (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP);\n",
        encoding="utf-8",
    )

    module = load_target_app_module(app_root)

    with TestClient(module.app) as client:
        healthy = client.get("/health/deep")
        assert healthy.status_code == 200
        assert healthy.json().get("ok") is True

        created = client.post("/widgets", json={"value": "hello"})
        assert created.status_code == 200
        assert created.json().get("ok") is True

        logs = client.get("/ops/logs", params={"lines": 50})
        assert logs.status_code == 200
        assert isinstance(logs.json().get("lines"), list)
        assert len(logs.json().get("lines", [])) >= 2

        fault = client.post("/ops/fault/drop_widgets_table")
        assert fault.status_code == 200

        unhealthy = client.get("/health/deep")
        assert unhealthy.status_code == 500
        assert unhealthy.json().get("detail", {}).get("error") == "schema_invalid"


def _app_root(tmp_path: Path) -> Path:
    app_root = tmp_path / "target-app"
    (app_root / "data").mkdir(parents=True, exist_ok=True)
    (app_root / "schema.sql").write_text(
        Path("target/app/schema.sql").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return app_root


def test_deep_health_rejects_widgets_table_with_missing_columns(tmp_path: Path) -> None:
    app_root = _app_root(tmp_path)
    module = load_target_app_module(app_root)

    with TestClient(module.app) as client:
        healthy = client.get("/ops/db/status").json()
        assert healthy["ok"] is True
        assert healthy["widgets_columns_missing"] == []

        # A repair MI has learned in the past: recreate widgets without created_at.
        with sqlite3.connect(app_root / "data" / "app.db") as conn:
            conn.execute("DROP TABLE widgets")
            conn.execute("CREATE TABLE IF NOT EXISTS widgets (id INTEGER PRIMARY KEY, name TEXT)")

        deep = client.get("/health/deep")
        assert deep.status_code == 500
        detail = deep.json()["detail"]
        assert detail["error"] == "schema_invalid"
        assert detail["status"]["widgets_table_present"] is True
        assert detail["status"]["widgets_columns_missing"] == ["created_at"]
        assert client.get("/widgets").status_code == 500  # what the health check now predicts


def test_required_columns_cover_every_widget_read(tmp_path: Path) -> None:
    module_source = Path("target/app/main.py").read_text(encoding="utf-8")
    selected = re.findall(r"SELECT (.+?) FROM widgets", module_source)
    assert selected, "no widget SELECT found"
    target = load_target_app_module(_app_root(tmp_path))
    for columns in selected:
        assert {c.strip() for c in columns.split(",")} <= target.REQUIRED_WIDGET_COLUMNS


def test_backup_finder_and_target_agree_on_required_columns(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "find_known_good_backup", Path("scripts/find_known_good_backup.py")
    )
    assert spec is not None and spec.loader is not None
    finder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(finder)
    target = load_target_app_module(_app_root(tmp_path))

    assert set(target.REQUIRED_WIDGET_COLUMNS) == finder.REQUIRED_COLUMNS
