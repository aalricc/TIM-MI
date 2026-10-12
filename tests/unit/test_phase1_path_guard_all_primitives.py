from __future__ import annotations

from pathlib import Path

import pytest

from mi_agent.errors import BackupAccessDeniedError, PathScopeError, ToolParameterValidationError
from mi_agent.events.bus import EventBus
from mi_agent.tools.executor import ToolExecutor

pytestmark = pytest.mark.phase1

_LONG_PATH = "a" * 600


def _cases_for(category: str) -> list[tuple[str, dict[str, object]]]:
    if category == "traversal":
        bad = "../../etc/passwd"
        return [
            ("list_directory", {"path": bad}),
            ("stat_path", {"path": bad}),
            ("read_app_file", {"path": bad}),
            ("create_directory", {"path": bad}),
            ("create_empty_sqlite_db", {"path": bad}),
            ("apply_schema", {"db_path": bad, "schema_path": "schema.sql"}),
            ("set_permissions", {"path": bad, "mode": 0o660}),
            ("check_db_integrity", {"db_path": bad}),
            ("probe_db_rw", {"db_path": bad}),
            ("inspect_paths", {"paths": [bad]}),
            (
                "recover_db_to_new_file",
                {"source_db_path": "data/app.db", "output_path": bad},
            ),
            (
                "move_path",
                {"source_path": bad, "destination_path": "data/x", "overwrite": True},
            ),
            ("write_app_file", {"path": bad, "content": "x"}),
            ("remove_path", {"path": bad}),
            ("reindex_db", {"db_path": bad}),
            ("checkpoint_wal", {"db_path": bad}),
        ]

    if category == "backup":
        bad = "../backups/forbidden.db"
        return [
            ("list_directory", {"path": "../backups"}),
            ("stat_path", {"path": bad}),
            ("read_app_file", {"path": bad}),
            ("create_directory", {"path": "../backups/newdir"}),
            ("create_empty_sqlite_db", {"path": bad}),
            ("apply_schema", {"db_path": bad, "schema_path": "schema.sql"}),
            ("set_permissions", {"path": bad, "mode": 0o660}),
            ("check_db_integrity", {"db_path": bad}),
            ("probe_db_rw", {"db_path": bad}),
            ("inspect_paths", {"paths": [bad]}),
            (
                "recover_db_to_new_file",
                {"source_db_path": "data/app.db", "output_path": bad},
            ),
            (
                "move_path",
                {"source_path": bad, "destination_path": "data/x", "overwrite": True},
            ),
            ("write_app_file", {"path": bad, "content": "x"}),
            ("remove_path", {"path": bad}),
            ("reindex_db", {"db_path": bad}),
            ("checkpoint_wal", {"db_path": bad}),
        ]

    if category == "oversize":
        return [
            ("list_directory", {"path": _LONG_PATH}),
            ("stat_path", {"path": _LONG_PATH}),
            ("read_app_file", {"path": _LONG_PATH}),
            ("create_directory", {"path": _LONG_PATH}),
            ("create_empty_sqlite_db", {"path": _LONG_PATH}),
            ("apply_schema", {"db_path": _LONG_PATH, "schema_path": "schema.sql"}),
            ("set_permissions", {"path": _LONG_PATH, "mode": 0o660}),
            ("check_db_integrity", {"db_path": _LONG_PATH}),
            ("probe_db_rw", {"db_path": _LONG_PATH}),
            ("inspect_paths", {"paths": [_LONG_PATH]}),
            (
                "recover_db_to_new_file",
                {"source_db_path": "data/app.db", "output_path": _LONG_PATH},
            ),
            (
                "move_path",
                {"source_path": _LONG_PATH, "destination_path": "data/x", "overwrite": True},
            ),
            ("write_app_file", {"path": _LONG_PATH, "content": "x"}),
            ("remove_path", {"path": _LONG_PATH}),
            ("reindex_db", {"db_path": _LONG_PATH}),
            ("checkpoint_wal", {"db_path": _LONG_PATH}),
        ]

    raise ValueError(category)


@pytest.mark.parametrize("primitive_id,params", _cases_for("traversal"))
def test_traversal_is_rejected_for_all_path_primitives(
    primitive_id: str,
    params: dict[str, object],
    test_settings,
) -> None:
    tools = ToolExecutor(test_settings, EventBus())
    with pytest.raises(PathScopeError):
        tools.validate_action_schema(primitive_id, params)


@pytest.mark.parametrize("primitive_id,params", _cases_for("backup"))
def test_backup_path_access_is_denied_for_all_path_primitives(
    primitive_id: str,
    params: dict[str, object],
    test_settings,
) -> None:
    tools = ToolExecutor(test_settings, EventBus())
    with pytest.raises(BackupAccessDeniedError):
        tools.validate_action_schema(primitive_id, params)


@pytest.mark.parametrize("primitive_id,params", _cases_for("oversize"))
def test_oversize_path_inputs_are_rejected_for_all_path_primitives(
    primitive_id: str,
    params: dict[str, object],
    test_settings,
) -> None:
    tools = ToolExecutor(test_settings, EventBus())
    with pytest.raises(ToolParameterValidationError):
        tools.validate_action_schema(primitive_id, params)


@pytest.mark.parametrize(
    "primitive_id,params",
    [
        ("list_directory", {"path": "data/outside_link"}),
        ("stat_path", {"path": "data/outside_link"}),
        ("read_app_file", {"path": "outside_link"}),
        ("create_directory", {"path": "data/outside_link/new"}),
        ("create_empty_sqlite_db", {"path": "data/outside_link/new.db"}),
        ("apply_schema", {"db_path": "data/outside_link/new.db", "schema_path": "schema.sql"}),
        ("set_permissions", {"path": "data/outside_link", "mode": 0o660}),
        ("check_db_integrity", {"db_path": "data/outside_link"}),
        ("probe_db_rw", {"db_path": "data/outside_link"}),
        ("inspect_paths", {"paths": ["data/outside_link"]}),
        (
            "recover_db_to_new_file",
            {"source_db_path": "data/app.db", "output_path": "data/outside_link/out.db"},
        ),
        (
            "move_path",
            {
                "source_path": "data/outside_link",
                "destination_path": "data/x",
                "overwrite": True,
            },
        ),
        ("write_app_file", {"path": "outside_link", "content": "x"}),
        ("remove_path", {"path": "data/outside_link"}),
        ("reindex_db", {"db_path": "data/outside_link"}),
        ("checkpoint_wal", {"db_path": "data/outside_link"}),
    ],
)
def test_symlink_escape_is_rejected_for_all_path_primitives(
    primitive_id: str,
    params: dict[str, object],
    test_settings,
) -> None:
    tools = ToolExecutor(test_settings, EventBus())
    outside_target = Path("/private/tmp/mi-outside-target")
    outside_target.mkdir(parents=True, exist_ok=True)

    data_link = test_settings.app_root / "data" / "outside_link"
    app_link = test_settings.app_root / "outside_link"
    for link in (data_link, app_link):
        if link.exists() or link.is_symlink():
            if link.is_dir() and not link.is_symlink():
                for child in link.iterdir():
                    if child.is_file() or child.is_symlink():
                        child.unlink()
                link.rmdir()
            else:
                link.unlink()
        link.symlink_to(outside_target)

    with pytest.raises(PathScopeError):
        tools.validate_action_schema(primitive_id, params)

