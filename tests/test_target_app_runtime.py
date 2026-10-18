from __future__ import annotations

import importlib.util
import os
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
