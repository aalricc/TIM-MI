from __future__ import annotations

from pathlib import Path

import pytest

from mi_agent.config import MISettings


@pytest.fixture
def settings(tmp_path: Path) -> MISettings:
    app_root = tmp_path / "app"
    backups_root = tmp_path / "backups"
    state_root = tmp_path / "state"
    app_root.mkdir(parents=True, exist_ok=True)
    backups_root.mkdir(parents=True, exist_ok=True)
    state_root.mkdir(parents=True, exist_ok=True)

    schema = app_root / "schema.sql"
    schema.write_text(
        "CREATE TABLE IF NOT EXISTS widgets(id INTEGER PRIMARY KEY);\n",
        encoding="utf-8",
    )

    return MISettings(
        openai_api_key="test-key",
        target_service_url="http://example.invalid",
        app_root=app_root,
        sandbox_root=tmp_path,
        backups_root=backups_root,
        db_file=app_root / "data" / "app.db",
        app_log=app_root / "app.log",
        schema_file=schema,
        state_dir=state_root,
        event_log_path=state_root / "events.jsonl",
        policy_store_path=state_root / "policy_store.json",
        tool_catalog_path=state_root / "tool_catalog.json",
    )
