from __future__ import annotations

import importlib.util
import os
import re
import sqlite3
from pathlib import Path
from types import ModuleType

from fastapi.testclient import TestClient


def load_target_app_module(
    tmp_root: Path, *, test_hooks: bool = True, backups_dir: Path | None = None
) -> ModuleType:
    os.environ["TARGET_APP_ROOT"] = tmp_root.as_posix()
    os.environ["TARGET_DB_PATH"] = (tmp_root / "data" / "app.db").as_posix()
    os.environ["TARGET_LOG_PATH"] = (tmp_root / "app.log").as_posix()
    os.environ["TARGET_SCHEMA_PATH"] = (tmp_root / "schema.sql").as_posix()
    os.environ["TARGET_HEARTBEAT_INTERVAL_SECONDS"] = "1"
    os.environ["TARGET_BACKUPS_DIR"] = (backups_dir or tmp_root / "backups").as_posix()
    os.environ["TARGET_TEST_HOOKS"] = "1" if test_hooks else "0"

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


def _write_backup(backups: Path, epoch: int, *, rows: list[str]) -> Path:
    backups.mkdir(parents=True, exist_ok=True)
    path = backups / f"app-{epoch}.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(Path("target/app/schema.sql").read_text(encoding="utf-8"))
        conn.executemany("INSERT INTO widgets(name) VALUES (?)", [(row,) for row in rows])
    return path


def test_backup_catalog_lists_kept_backups_newest_first(tmp_path: Path) -> None:
    app_root = _app_root(tmp_path)
    backups = app_root / "backups"
    _write_backup(backups, 1792854880, rows=["a"])
    _write_backup(backups, 1792854890, rows=["a", "b"])
    (backups / ".app-1792854900.db.tmp").write_bytes(b"partial copy")
    (backups / "notes.txt").write_text("not a backup", encoding="utf-8")
    module = load_target_app_module(app_root)

    with TestClient(module.app) as client:
        payload = client.get("/ops/backups").json()
        limited = client.get("/ops/backups", params={"limit": 1}).json()

    assert payload["ok"] is True
    assert [row["backup_id"] for row in payload["backups"]] == ["app-1792854890", "app-1792854880"]
    assert payload["backups"][0]["created_at"] == "2026-10-24T15:14:50+00:00"
    assert payload["backups"][0]["size_bytes"] > 0
    assert set(payload["backups"][0]) == {"backup_id", "created_at", "size_bytes"}
    assert [row["backup_id"] for row in limited["backups"]] == ["app-1792854890"]


def test_backup_catalog_is_empty_without_backups_dir(tmp_path: Path) -> None:
    module = load_target_app_module(_app_root(tmp_path), backups_dir=tmp_path / "missing")
    with TestClient(module.app) as client:
        assert client.get("/ops/backups").json() == {"ok": True, "backups": []}


def test_restore_backup_replaces_database(tmp_path: Path) -> None:
    app_root = _app_root(tmp_path)
    _write_backup(app_root / "backups", 1792854880, rows=["kept"])
    module = load_target_app_module(app_root)

    with TestClient(module.app) as client:
        assert client.post("/ops/fault/corrupt_db").status_code == 200
        assert client.get("/health/deep").status_code == 500

        restored = client.post("/ops/backups/app-1792854880/restore")

        assert restored.status_code == 200
        assert restored.json() == {"ok": True, "backup_id": "app-1792854880"}
        assert client.get("/health/deep").status_code == 200
        assert [item["value"] for item in client.get("/widgets").json()["items"]] == ["kept"]
    assert not (app_root / "data" / ".app.db.restore").exists()


def test_restore_unknown_backup_lists_available_ids(tmp_path: Path) -> None:
    app_root = _app_root(tmp_path)
    _write_backup(app_root / "backups", 1792854880, rows=[])
    _write_backup(app_root / "backups", 1792854890, rows=[])
    module = load_target_app_module(app_root)

    with TestClient(module.app) as client:
        before = (app_root / "data" / "app.db").read_bytes()
        response = client.post("/ops/backups/latest_pre_fault/restore")

    assert response.status_code == 404
    assert response.json()["detail"] == {
        "ok": False,
        "error": "backup_not_found",
        "available": ["app-1792854890", "app-1792854880"],
    }
    assert (app_root / "data" / "app.db").read_bytes() == before


def test_restore_rejects_ids_outside_backup_names(tmp_path: Path) -> None:
    app_root = _app_root(tmp_path)
    (app_root / "backups").mkdir()
    (tmp_path / "app-1.db").write_bytes(b"outside the backups dir")
    module = load_target_app_module(app_root)

    with TestClient(module.app) as client:
        for backup_id in ("..%2Fapp-1", "app-1.db", "app-x"):
            response = client.post(f"/ops/backups/{backup_id}/restore")
            assert response.status_code in {404, 405}, backup_id


def test_test_hooks_are_not_registered_by_default(tmp_path: Path) -> None:
    module = load_target_app_module(_app_root(tmp_path), test_hooks=False)

    with TestClient(module.app) as client:
        assert client.post("/ops/fault/drop_widgets_table").status_code == 404
        assert client.post("/ops/reset").status_code == 404
        assert client.get("/health/deep").status_code == 200


def test_reset_rebuilds_a_healthy_database(tmp_path: Path) -> None:
    app_root = _app_root(tmp_path)
    module = load_target_app_module(app_root)

    with TestClient(module.app) as client:
        for fault in ("corrupt_db", "delete_db", "drop_widgets_table"):
            if fault == "drop_widgets_table":
                assert client.post("/ops/reset").status_code == 200
            assert client.post(f"/ops/fault/{fault}").status_code == 200
            assert client.get("/health/deep").status_code == 500

            assert client.post("/ops/reset").json() == {"ok": True}
            assert client.get("/health/deep").status_code == 200
