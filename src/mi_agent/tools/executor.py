from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from shutil import copy2, copytree, rmtree
from stat import S_IMODE
from types import MappingProxyType
from typing import Any, Literal, cast
from uuid import uuid4

import httpx
from pydantic import BaseModel, ValidationError

from mi_agent.config import MISettings
from mi_agent.errors import (
    PrimitiveNotAllowedError,
    ToolExecutionError,
    ToolParameterValidationError,
)
from mi_agent.events.bus import EventBus
from mi_agent.events.models import Event
from mi_agent.tools.pathing import PathGuard
from mi_agent.tools.schemas import (
    ApplySchemaParams,
    CheckDBIntegrityParams,
    CheckpointWALParams,
    CreateDirectoryParams,
    CreateEmptySQLiteDBParams,
    EmptyParams,
    HealthDeepProbeParams,
    InspectPathsParams,
    InspectRuntimeConfigParams,
    ListBackupsParams,
    ListDirectoryParams,
    MovePathParams,
    ProbeDBRWParams,
    ReadAppFileParams,
    ReadAppLogParams,
    RecoverDBToNewFileParams,
    ReindexDBParams,
    RemovePathParams,
    RequestBackupRestoreParams,
    RestartServiceParams,
    SetPermissionsParams,
    StatPathParams,
    WaitParams,
    WriteAppFileParams,
)


@dataclass(frozen=True, slots=True)
class PrimitiveSpec:
    primitive_id: str
    params_model: type[BaseModel]
    mutating: bool
    reversible: bool
    risk_tier: Literal["read_only", "reversible", "irreversible", "terminal", "brokered"]
    description: str


@dataclass(frozen=True, slots=True)
class ToolSnapshot:
    snapshot_id: str
    db_existed: bool
    db_backup_path: Path | None
    path_snapshots: tuple[PathSnapshot, ...] = ()


@dataclass(frozen=True, slots=True)
class PathSnapshot:
    path: Path
    existed: bool
    kind: Literal["file", "directory", "symlink", "missing"]
    backup_path: Path | None
    symlink_target: str | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    primitive_id: str
    data: dict[str, Any]
    snapshot: ToolSnapshot | None


class ToolExecutor:
    _MAX_PATH_PARAM_LEN = 512

    def __init__(
        self,
        settings: MISettings,
        event_bus: EventBus,
        restart_service_hook: Callable[[str], Awaitable[None]] | None = None,
        request_backup_restore_hook: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self._settings = settings
        self._event_bus = event_bus
        self._path_guard = PathGuard(settings)
        self._restart_service_hook = restart_service_hook
        self._request_backup_restore_hook = request_backup_restore_hook
        self._snapshot_dir = settings.state_dir / "snapshots"
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._specs = MappingProxyType(
            {
                "ping_db": PrimitiveSpec(
                    "ping_db",
                    EmptyParams,
                    False,
                    False,
                    "read_only",
                    "Checks whether the configured SQLite database accepts a basic query.",
                ),
                "read_app_log": PrimitiveSpec(
                    "read_app_log",
                    ReadAppLogParams,
                    False,
                    False,
                    "read_only",
                    "Reads the requested number of trailing lines from the application log file.",
                ),
                "list_directory": PrimitiveSpec(
                    "list_directory",
                    ListDirectoryParams,
                    False,
                    False,
                    "read_only",
                    "Lists entry names in a directory inside sandbox scope.",
                ),
                "stat_path": PrimitiveSpec(
                    "stat_path",
                    StatPathParams,
                    False,
                    False,
                    "read_only",
                    "Returns file-system metadata for a sandbox-scoped path.",
                ),
                "read_app_file": PrimitiveSpec(
                    "read_app_file",
                    ReadAppFileParams,
                    False,
                    False,
                    "read_only",
                    "Reads text content from an application-root file path.",
                ),
                "check_schema": PrimitiveSpec(
                    "check_schema",
                    EmptyParams,
                    False,
                    False,
                    "read_only",
                    "Compares expected table names from schema SQL against current DB tables.",
                ),
                "check_db_integrity": PrimitiveSpec(
                    "check_db_integrity",
                    CheckDBIntegrityParams,
                    False,
                    False,
                    "read_only",
                    (
                        "Runs SQLite integrity_check on the specified database "
                        "and returns result lines."
                    ),
                ),
                "probe_db_rw": PrimitiveSpec(
                    "probe_db_rw",
                    ProbeDBRWParams,
                    False,
                    False,
                    "read_only",
                    "Executes a transactional write/read probe and reports rollback outcomes.",
                ),
                "inspect_runtime_config": PrimitiveSpec(
                    "inspect_runtime_config",
                    InspectRuntimeConfigParams,
                    False,
                    False,
                    "read_only",
                    "Returns selected effective runtime configuration values.",
                ),
                "health_deep_probe": PrimitiveSpec(
                    "health_deep_probe",
                    HealthDeepProbeParams,
                    False,
                    False,
                    "read_only",
                    "Calls the deep health endpoint and returns parsed response fields.",
                ),
                "inspect_paths": PrimitiveSpec(
                    "inspect_paths",
                    InspectPathsParams,
                    False,
                    False,
                    "read_only",
                    "Returns existence and metadata for each requested sandbox path.",
                ),
                "list_backups": PrimitiveSpec(
                    "list_backups",
                    ListBackupsParams,
                    False,
                    False,
                    "read_only",
                    "Lists backup metadata by id, timestamp, size and verification status.",
                ),
                "request_backup_restore": PrimitiveSpec(
                    "request_backup_restore",
                    RequestBackupRestoreParams,
                    False,
                    False,
                    "brokered",
                    (
                        "Requests supervisor-managed restore by backup id "
                        "without exposing backup paths."
                    ),
                ),
                "wait": PrimitiveSpec(
                    "wait",
                    WaitParams,
                    False,
                    False,
                    "read_only",
                    "Sleeps for the bounded duration and returns the elapsed interval.",
                ),
                "reindex_db": PrimitiveSpec(
                    "reindex_db",
                    ReindexDBParams,
                    True,
                    True,
                    "reversible",
                    "Rebuilds SQLite index structures for the specified database.",
                ),
                "checkpoint_wal": PrimitiveSpec(
                    "checkpoint_wal",
                    CheckpointWALParams,
                    True,
                    True,
                    "reversible",
                    "Runs a SQLite WAL checkpoint for the specified database.",
                ),
                "recover_db_to_new_file": PrimitiveSpec(
                    "recover_db_to_new_file",
                    RecoverDBToNewFileParams,
                    True,
                    True,
                    "reversible",
                    "Copies recoverable DB contents into a separate destination database file.",
                ),
                "move_path": PrimitiveSpec(
                    "move_path",
                    MovePathParams,
                    True,
                    True,
                    "reversible",
                    "Moves or renames a sandbox path to a destination path.",
                ),
                "write_app_file": PrimitiveSpec(
                    "write_app_file",
                    WriteAppFileParams,
                    True,
                    True,
                    "reversible",
                    "Replaces text contents of an application-root file path.",
                ),
                "remove_path": PrimitiveSpec(
                    "remove_path",
                    RemovePathParams,
                    True,
                    False,
                    "irreversible",
                    "Deletes a sandbox file or directory path.",
                ),
                "create_directory": PrimitiveSpec(
                    "create_directory",
                    CreateDirectoryParams,
                    True,
                    False,
                    "irreversible",
                    "Creates a directory path inside sandbox scope.",
                ),
                "create_empty_sqlite_db": PrimitiveSpec(
                    "create_empty_sqlite_db",
                    CreateEmptySQLiteDBParams,
                    True,
                    True,
                    "reversible",
                    "Creates an empty SQLite database file at the requested path.",
                ),
                "apply_schema": PrimitiveSpec(
                    "apply_schema",
                    ApplySchemaParams,
                    True,
                    True,
                    "reversible",
                    "Executes schema SQL against the specified SQLite database.",
                ),
                "set_permissions": PrimitiveSpec(
                    "set_permissions",
                    SetPermissionsParams,
                    True,
                    True,
                    "reversible",
                    "Applies the provided file mode to the allowed DB path target.",
                ),
                "restart_service": PrimitiveSpec(
                    "restart_service",
                    RestartServiceParams,
                    True,
                    False,
                    "irreversible",
                    "Invokes the configured service restart hook for the named service.",
                ),
            }
        )

    @property
    def primitive_ids(self) -> frozenset[str]:
        return frozenset(self._specs.keys())

    def is_mutating(self, primitive_id: str) -> bool:
        spec = self._specs.get(primitive_id)
        if spec is None:
            raise PrimitiveNotAllowedError(f"Unknown primitive: {primitive_id}")
        return spec.mutating

    def risk_tier(self, primitive_id: str) -> str:
        spec = self._specs.get(primitive_id)
        if spec is None:
            raise PrimitiveNotAllowedError(f"Unknown primitive: {primitive_id}")
        return spec.risk_tier

    def primitive_parameter_contracts(self) -> dict[str, dict[str, Any]]:
        contracts: dict[str, dict[str, Any]] = {}
        for primitive_id, spec in self._specs.items():
            schema = spec.params_model.model_json_schema()
            properties = schema.get("properties")
            required = schema.get("required")
            contracts[primitive_id] = {
                "required": required if isinstance(required, list) else [],
                "properties": properties if isinstance(properties, dict) else {},
                "mutating": spec.mutating,
                "reversible": spec.reversible,
                "risk_tier": spec.risk_tier,
                "description": spec.description,
            }
        return contracts

    def with_default_parameters(
        self,
        primitive_id: str,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        spec = self._specs.get(primitive_id)
        if spec is None:
            raise PrimitiveNotAllowedError(f"Unknown primitive: {primitive_id}")
        merged = self._default_parameters(primitive_id)
        merged.update(parameters)
        return merged

    def _default_parameters(self, primitive_id: str) -> dict[str, Any]:
        db_file_rel = self._relative_path(self._settings.db_file, self._settings.sandbox_root)
        db_dir_rel = self._relative_path(self._settings.db_file.parent, self._settings.sandbox_root)
        schema_rel = self._relative_path(self._settings.schema_file, self._settings.app_root)

        defaults: dict[str, dict[str, Any]] = {
            "list_directory": {"path": db_dir_rel},
            "stat_path": {"path": db_file_rel},
            "read_app_file": {"path": schema_rel},
            "create_directory": {"path": db_dir_rel},
            "set_permissions": {"path": db_file_rel, "mode": 0o660},
        }
        return defaults.get(primitive_id, {}).copy()

    @staticmethod
    def _relative_path(path: Path, root: Path) -> str:
        root_resolved = root.resolve(strict=False)
        path_resolved = path.resolve(strict=False)
        try:
            relative = path_resolved.relative_to(root_resolved)
        except ValueError:
            return path.as_posix()
        if relative.as_posix() == ".":
            return "."
        return relative.as_posix()

    async def execute(
        self,
        primitive_id: str,
        parameters: dict[str, Any],
        *,
        allowed_primitives: frozenset[str] | None = None,
    ) -> ToolExecutionResult:
        spec = self._specs.get(primitive_id)
        if spec is None:
            raise PrimitiveNotAllowedError(f"Unknown primitive: {primitive_id}")
        if allowed_primitives is not None and primitive_id not in allowed_primitives:
            raise PrimitiveNotAllowedError(
                f"Primitive {primitive_id} not permitted for this incident"
            )
        validated_params = self.validate_action_schema(primitive_id, parameters)
        snapshot = await self._pre_action_snapshot(spec, primitive_id, validated_params)
        try:
            data = await self._run_primitive(spec.primitive_id, validated_params)
        except sqlite3.Error as exc:
            raise ToolExecutionError(
                f"{primitive_id} failed: {exc}"
            ) from exc
        await self._event_bus.publish(
            Event(
                event_type="tool.executed",
                payload={
                    "primitive_id": primitive_id,
                    "mutating": spec.mutating,
                    "reversible": spec.reversible,
                },
            )
        )
        return ToolExecutionResult(primitive_id=primitive_id, data=data, snapshot=snapshot)

    async def rollback(self, snapshot: ToolSnapshot) -> None:
        for path_snapshot in snapshot.path_snapshots:
            self._restore_path_snapshot(path_snapshot)

        db_path = self._settings.db_file
        if snapshot.db_existed:
            if snapshot.db_backup_path is not None and snapshot.db_backup_path.exists():
                db_path.parent.mkdir(parents=True, exist_ok=True)
                self._remove_existing_path(db_path)
                copy2(snapshot.db_backup_path, db_path)
        else:
            self._remove_existing_path(db_path)

        await self._event_bus.publish(
            Event(event_type="tool.rollback", payload={"snapshot_id": snapshot.snapshot_id})
        )

    def _restore_path_snapshot(self, snapshot: PathSnapshot) -> None:
        target = snapshot.path

        if not snapshot.existed:
            self._remove_existing_path(target)
            return

        self._remove_existing_path(target)
        target.parent.mkdir(parents=True, exist_ok=True)

        if snapshot.kind == "symlink":
            if snapshot.symlink_target is not None:
                target.symlink_to(snapshot.symlink_target)
            return

        if snapshot.kind == "directory":
            if snapshot.backup_path is not None:
                copytree(snapshot.backup_path, target)
            return

        if snapshot.kind == "file" and snapshot.backup_path is not None:
            copy2(snapshot.backup_path, target)

    @staticmethod
    def _remove_existing_path(target: Path) -> None:
        if not target.exists() and not target.is_symlink():
            return
        if target.is_symlink() or target.is_file():
            target.unlink()
            return
        if target.is_dir():
            rmtree(target)

    def _validate_params(
        self, params_model: type[BaseModel], parameters: dict[str, Any]
    ) -> BaseModel:
        try:
            return params_model.model_validate(parameters)
        except ValidationError as exc:
            raise ToolParameterValidationError(str(exc)) from exc

    def validate_action_schema(self, primitive_id: str, parameters: dict[str, Any]) -> BaseModel:
        spec = self._specs.get(primitive_id)
        if spec is None:
            raise PrimitiveNotAllowedError(f"Unknown primitive: {primitive_id}")
        validated = self._validate_params(spec.params_model, parameters)
        self._validate_param_paths(primitive_id, validated)
        return validated

    def _validate_param_paths(self, primitive_id: str, validated: BaseModel) -> None:
        data = validated.model_dump()
        path_value = data.get("path")
        if isinstance(path_value, str):
            self._validate_path_length(path_value)
            if primitive_id in {"read_app_file", "write_app_file"}:
                self._path_guard.resolve_app_file(path_value)
            else:
                self._path_guard.resolve_in_sandbox(path_value)
        db_path = data.get("db_path")
        if isinstance(db_path, str):
            self._validate_path_length(db_path)
            self._path_guard.resolve_in_sandbox(db_path)
        source_db_path = data.get("source_db_path")
        if isinstance(source_db_path, str):
            self._validate_path_length(source_db_path)
            self._path_guard.resolve_in_sandbox(source_db_path)
        output_path = data.get("output_path")
        if isinstance(output_path, str):
            self._validate_path_length(output_path)
            self._path_guard.resolve_in_sandbox(output_path)
        source_path = data.get("source_path")
        if isinstance(source_path, str):
            self._validate_path_length(source_path)
            self._path_guard.resolve_in_sandbox(source_path)
        destination_path = data.get("destination_path")
        if isinstance(destination_path, str):
            self._validate_path_length(destination_path)
            self._path_guard.resolve_in_sandbox(destination_path)
        schema_path = data.get("schema_path")
        if isinstance(schema_path, str):
            self._validate_path_length(schema_path)
            self._path_guard.resolve_app_file(schema_path)
        paths_value = data.get("paths")
        if isinstance(paths_value, list):
            for item in paths_value:
                if isinstance(item, str):
                    self._validate_path_length(item)
                    self._path_guard.resolve_in_sandbox(item)

    def _validate_path_length(self, value: str) -> None:
        if len(value) > self._MAX_PATH_PARAM_LEN:
            raise ToolParameterValidationError("Path parameter exceeds maximum length")

    async def _pre_action_snapshot(
        self,
        spec: PrimitiveSpec,
        primitive_id: str,
        params: BaseModel,
    ) -> ToolSnapshot | None:
        if not spec.mutating:
            return None
        db_path = self._settings.db_file
        existed = db_path.exists()
        backup_path: Path | None = None
        path_snapshots: list[PathSnapshot] = []
        if existed:
            snapshot_id = str(uuid4())
            backup_path = self._snapshot_dir / f"{snapshot_id}.db"
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                copy2(db_path, backup_path)
            except PermissionError:
                self._ensure_db_snapshot_readable(db_path)
                copy2(db_path, backup_path)
        else:
            snapshot_id = str(uuid4())

        if spec.reversible:
            path_snapshots = self._collect_reversible_path_snapshots(primitive_id, params)

        return ToolSnapshot(
            snapshot_id=snapshot_id,
            db_existed=existed,
            db_backup_path=backup_path,
            path_snapshots=tuple(path_snapshots),
        )

    @staticmethod
    def _ensure_db_snapshot_readable(db_path: Path) -> None:
        try:
            current = S_IMODE(db_path.stat().st_mode)
            db_path.chmod(current | 0o600)
        except OSError:
            return

    def _collect_reversible_path_snapshots(
        self,
        primitive_id: str,
        params: BaseModel,
    ) -> list[PathSnapshot]:
        data = params.model_dump()
        targets: list[Path] = []
        if primitive_id == "move_path":
            source = self._path_guard.resolve_in_sandbox(str(data["source_path"]))
            destination = self._path_guard.resolve_in_sandbox(str(data["destination_path"]))
            targets.extend([source, destination])
        elif primitive_id == "set_permissions":
            targets.append(self._path_guard.resolve_in_sandbox(str(data["path"])))
        elif primitive_id == "write_app_file":
            targets.append(self._path_guard.resolve_app_file(str(data["path"])))
        elif primitive_id == "recover_db_to_new_file":
            targets.append(self._path_guard.resolve_in_sandbox(str(data["output_path"])))

        snapshots: list[PathSnapshot] = []
        seen: set[Path] = set()
        for target in targets:
            if target in seen:
                continue
            seen.add(target)
            snapshots.append(self._snapshot_path(target))
        return snapshots

    def _snapshot_path(self, target: Path) -> PathSnapshot:
        if not target.exists() and not target.is_symlink():
            return PathSnapshot(
                path=target,
                existed=False,
                kind="missing",
                backup_path=None,
            )

        snapshot_id = str(uuid4())
        backup_base = self._snapshot_dir / "paths"
        backup_base.mkdir(parents=True, exist_ok=True)
        backup_path = backup_base / snapshot_id

        if target.is_symlink():
            return PathSnapshot(
                path=target,
                existed=True,
                kind="symlink",
                backup_path=None,
                symlink_target=target.readlink().as_posix(),
            )

        if target.is_dir():
            copytree(target, backup_path)
            return PathSnapshot(
                path=target,
                existed=True,
                kind="directory",
                backup_path=backup_path,
            )

        copy2(target, backup_path)
        return PathSnapshot(
            path=target,
            existed=True,
            kind="file",
            backup_path=backup_path,
        )

    async def _run_primitive(self, primitive_id: str, params: BaseModel) -> dict[str, Any]:
        if primitive_id == "ping_db":
            return await self._ping_db()
        if primitive_id == "read_app_log":
            log_params = cast(ReadAppLogParams, params)
            return await self._read_app_log(log_params.lines)
        if primitive_id == "list_directory":
            list_params = cast(ListDirectoryParams, params)
            return await self._list_directory(list_params.path)
        if primitive_id == "stat_path":
            stat_params = cast(StatPathParams, params)
            return await self._stat_path(stat_params.path)
        if primitive_id == "read_app_file":
            read_params = cast(ReadAppFileParams, params)
            return await self._read_app_file(read_params.path)
        if primitive_id == "check_schema":
            return {"schema_ok": await self._check_schema()}
        if primitive_id == "check_db_integrity":
            integrity_params = cast(CheckDBIntegrityParams, params)
            return await self._check_db_integrity(integrity_params.db_path)
        if primitive_id == "probe_db_rw":
            probe_params = cast(ProbeDBRWParams, params)
            return await self._probe_db_rw(probe_params.db_path)
        if primitive_id == "inspect_runtime_config":
            return await self._inspect_runtime_config()
        if primitive_id == "health_deep_probe":
            return await self._health_deep_probe()
        if primitive_id == "inspect_paths":
            inspect_params = cast(InspectPathsParams, params)
            return await self._inspect_paths(inspect_params.paths)
        if primitive_id == "list_backups":
            backups_params = cast(ListBackupsParams, params)
            return await self._list_backups(backups_params.limit)
        if primitive_id == "request_backup_restore":
            restore_params = cast(RequestBackupRestoreParams, params)
            return await self._request_backup_restore(restore_params.backup_id)
        if primitive_id == "wait":
            wait_params = cast(WaitParams, params)
            return await self._wait(wait_params.seconds)
        if primitive_id == "reindex_db":
            reindex_params = cast(ReindexDBParams, params)
            return await self._reindex_db(reindex_params.db_path)
        if primitive_id == "checkpoint_wal":
            checkpoint_params = cast(CheckpointWALParams, params)
            return await self._checkpoint_wal(checkpoint_params.db_path)
        if primitive_id == "recover_db_to_new_file":
            recover_params = cast(RecoverDBToNewFileParams, params)
            return await self._recover_db_to_new_file(
                recover_params.source_db_path,
                recover_params.output_path,
            )
        if primitive_id == "move_path":
            move_params = cast(MovePathParams, params)
            return await self._move_path(
                move_params.source_path,
                move_params.destination_path,
                move_params.overwrite,
            )
        if primitive_id == "write_app_file":
            write_params = cast(WriteAppFileParams, params)
            return await self._write_app_file(write_params.path, write_params.content)
        if primitive_id == "remove_path":
            remove_params = cast(RemovePathParams, params)
            return await self._remove_path(remove_params.path, remove_params.recursive)
        if primitive_id == "create_directory":
            mkdir_params = cast(CreateDirectoryParams, params)
            return await self._create_directory(mkdir_params.path)
        if primitive_id == "create_empty_sqlite_db":
            create_db_params = cast(CreateEmptySQLiteDBParams, params)
            return await self._create_empty_sqlite_db(create_db_params.path)
        if primitive_id == "apply_schema":
            apply_params = cast(ApplySchemaParams, params)
            return await self._apply_schema(apply_params.db_path, apply_params.schema_path)
        if primitive_id == "set_permissions":
            chmod_params = cast(SetPermissionsParams, params)
            return await self._set_permissions(chmod_params.path, chmod_params.mode)
        if primitive_id == "restart_service":
            restart_params = cast(RestartServiceParams, params)
            return await self._restart_service(restart_params.service_name)
        raise PrimitiveNotAllowedError(f"Primitive not implemented: {primitive_id}")

    async def _ping_db(self) -> dict[str, Any]:
        db_path = self._settings.db_file

        def _sync_ping() -> bool:
            if not db_path.exists():
                return False
            with sqlite3.connect(db_path) as conn:
                conn.execute("SELECT 1")
            return True

        try:
            ok = await asyncio.to_thread(_sync_ping)
            return {"ok": ok}
        except sqlite3.Error:
            return {"ok": False}

    async def _read_app_log(self, lines: int) -> dict[str, Any]:
        relative = self._settings.app_log.relative_to(self._settings.app_root).as_posix()
        log_path = self._path_guard.resolve_app_file(relative)
        if not log_path.exists():
            return {"lines": []}
        content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return {"lines": content[-lines:]}

    async def _list_directory(self, relative_path: str) -> dict[str, Any]:
        target = self._path_guard.resolve_in_sandbox(relative_path)
        if not target.exists() or not target.is_dir():
            return {"entries": []}
        entries = sorted(item.name for item in target.iterdir())
        return {"entries": entries}

    async def _stat_path(self, relative_path: str) -> dict[str, Any]:
        target = self._path_guard.resolve_in_sandbox(relative_path)
        exists = target.exists()
        if not exists:
            return {"exists": False}
        stat = target.stat()
        return {
            "exists": True,
            "is_file": target.is_file(),
            "is_dir": target.is_dir(),
            "mode": oct(S_IMODE(stat.st_mode)),
            "size": stat.st_size,
        }

    async def _read_app_file(self, relative_path: str) -> dict[str, Any]:
        target = self._path_guard.resolve_app_file(relative_path)
        if not target.exists() or not target.is_file():
            return {"content": "", "exists": False}
        text = target.read_text(encoding="utf-8", errors="replace")
        return {"content": text[:20000], "exists": True}

    async def _check_schema(self) -> bool:
        schema_path = self._settings.schema_file
        db_path = self._settings.db_file
        if not db_path.exists() or not schema_path.exists():
            return False

        expected_tables = self._extract_expected_tables(schema_path.read_text(encoding="utf-8"))

        def _sync_tables() -> set[str]:
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            return {str(row[0]) for row in rows}

        try:
            actual_tables = await asyncio.to_thread(_sync_tables)
        except sqlite3.Error:
            return False
        return expected_tables.issubset(actual_tables)

    async def _check_db_integrity(self, db_relative_path: str) -> dict[str, Any]:
        db_path = self._path_guard.resolve_in_sandbox(db_relative_path)
        if not db_path.exists():
            return {"ok": False, "result_lines": ["database file missing"]}

        def _sync_integrity() -> list[str]:
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute("PRAGMA integrity_check;").fetchall()
            return [str(row[0]) for row in rows]

        try:
            lines = await asyncio.to_thread(_sync_integrity)
        except sqlite3.Error as exc:
            return {"ok": False, "result_lines": [str(exc)]}
        ok = len(lines) == 1 and lines[0].strip().lower() == "ok"
        return {"ok": ok, "result_lines": lines}

    async def _probe_db_rw(self, db_relative_path: str) -> dict[str, Any]:
        db_path = self._path_guard.resolve_in_sandbox(db_relative_path)
        if not db_path.exists():
            return {
                "db_exists": False,
                "read_ok": False,
                "write_ok": False,
                "rolled_back": False,
            }

        def _sync_probe() -> dict[str, Any]:
            table_exists = False
            baseline_count = 0
            with sqlite3.connect(db_path) as conn:
                table_row = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='__mi_probe_rw'"
                ).fetchone()
                table_exists = table_row is not None
                if table_exists:
                    baseline_count = int(
                        conn.execute("SELECT COUNT(*) FROM __mi_probe_rw").fetchone()[0]
                    )
                conn.execute("BEGIN IMMEDIATE")
                if not table_exists:
                    conn.execute(
                        "CREATE TABLE __mi_probe_rw (id INTEGER PRIMARY KEY, v TEXT NOT NULL)"
                    )
                conn.execute("INSERT INTO __mi_probe_rw(v) VALUES ('probe')")
                row = conn.execute(
                    "SELECT v FROM __mi_probe_rw ORDER BY id DESC LIMIT 1"
                ).fetchone()
                conn.execute("ROLLBACK")

            with sqlite3.connect(db_path) as conn:
                table_row_after = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='__mi_probe_rw'"
                ).fetchone()
                table_exists_after = table_row_after is not None
                if table_exists_after:
                    final_count = int(
                        conn.execute("SELECT COUNT(*) FROM __mi_probe_rw").fetchone()[0]
                    )
                else:
                    final_count = 0

            rolled_back = (
                final_count == baseline_count if table_exists else (not table_exists_after)
            )
            return {
                "db_exists": True,
                "read_ok": row is not None and str(row[0]) == "probe",
                "write_ok": True,
                "rolled_back": rolled_back,
            }

        try:
            return await asyncio.to_thread(_sync_probe)
        except sqlite3.Error:
            return {
                "db_exists": True,
                "read_ok": False,
                "write_ok": False,
                "rolled_back": False,
            }

    async def _inspect_runtime_config(self) -> dict[str, Any]:
        settings = self._settings
        return {
            "config": {
                "monitor_interval_seconds": settings.monitor_interval_seconds,
                "max_attempts_per_incident": settings.max_attempts_per_incident,
                "max_incident_seconds": settings.max_incident_seconds,
                "supervisor_backoff_initial_seconds": settings.supervisor_backoff_initial_seconds,
                "supervisor_backoff_max_seconds": settings.supervisor_backoff_max_seconds,
                "target_service_url": settings.target_service_url,
                "llm_model": settings.llm_model,
                "llm_timeout_seconds": settings.llm_timeout_seconds,
                "llm_retries": settings.llm_retries,
                "paths": {
                    "app_root": settings.app_root.as_posix(),
                    "sandbox_root": settings.sandbox_root.as_posix(),
                    "db_file": settings.db_file.as_posix(),
                    "app_log": settings.app_log.as_posix(),
                    "schema_file": settings.schema_file.as_posix(),
                    "state_dir": settings.state_dir.as_posix(),
                },
                "backups_root_configured": True,
            }
        }

    async def _health_deep_probe(self) -> dict[str, Any]:
        url = f"{self._settings.target_service_url}/health/deep"
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(url)
            payload: dict[str, Any]
            try:
                raw_payload = response.json()
                payload = raw_payload if isinstance(raw_payload, dict) else {"raw": raw_payload}
            except ValueError:
                payload = {"raw": ""}
            return {
                "status_code": response.status_code,
                "ok": bool(payload.get("ok", False)),
                "fields": payload,
            }
        except httpx.HTTPError as exc:
            return {"status_code": None, "ok": False, "fields": {"error": str(exc)}}

    async def _inspect_paths(self, input_paths: list[str]) -> dict[str, Any]:
        details: list[dict[str, Any]] = []
        for raw_path in input_paths:
            self._path_guard.resolve_in_sandbox(raw_path)
            target = self._settings.app_root / raw_path
            if not target.exists() and not target.is_symlink():
                details.append(
                    {
                        "input_path": raw_path,
                        "exists": False,
                        "type": "missing",
                        "mode": None,
                        "size": None,
                        "link_target": None,
                    }
                )
                continue

            stat = target.lstat()
            if target.is_symlink():
                path_type = "symlink"
                link_target = target.readlink().as_posix()
            elif target.is_file():
                path_type = "file"
                link_target = None
            elif target.is_dir():
                path_type = "directory"
                link_target = None
            else:
                path_type = "other"
                link_target = None

            details.append(
                {
                    "input_path": raw_path,
                    "exists": True,
                    "type": path_type,
                    "mode": oct(S_IMODE(stat.st_mode)),
                    "size": stat.st_size,
                    "link_target": link_target,
                }
            )
        return {"paths": details}

    async def _list_backups(self, limit: int) -> dict[str, Any]:
        backups_root = self._settings.backups_root
        backups_root.mkdir(parents=True, exist_ok=True)

        candidates = sorted(
            backups_root.glob("*.db"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        output: list[dict[str, Any]] = []
        for candidate in candidates[:limit]:
            stat = candidate.stat()
            backup_id = candidate.stem
            timestamp = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
            verified = await asyncio.to_thread(self._is_valid_sqlite_backup, candidate)
            output.append(
                {
                    "backup_id": backup_id,
                    "timestamp": timestamp,
                    "size_bytes": stat.st_size,
                    "verified": verified,
                }
            )
        return {"backups": output}

    async def _request_backup_restore(self, backup_id: str) -> dict[str, Any]:
        if self._request_backup_restore_hook is None:
            raise ToolParameterValidationError("Brokered restore hook is not configured")
        return await self._request_backup_restore_hook(backup_id)

    @staticmethod
    def _is_valid_sqlite_backup(path: Path) -> bool:
        if not path.exists() or path.stat().st_size == 0:
            return False
        try:
            with sqlite3.connect(path) as conn:
                row = conn.execute("PRAGMA integrity_check;").fetchone()
            return row is not None and str(row[0]).strip().lower() == "ok"
        except sqlite3.Error:
            return False

    async def _wait(self, seconds: float) -> dict[str, Any]:
        await asyncio.sleep(seconds)
        return {"waited_seconds": seconds}

    async def _reindex_db(self, db_relative_path: str) -> dict[str, Any]:
        db_path = self._path_guard.resolve_in_sandbox(db_relative_path)

        def _sync_reindex() -> None:
            with sqlite3.connect(db_path) as conn:
                conn.execute("REINDEX")

        await asyncio.to_thread(_sync_reindex)
        return {"reindexed": True}

    async def _checkpoint_wal(self, db_relative_path: str) -> dict[str, Any]:
        db_path = self._path_guard.resolve_in_sandbox(db_relative_path)

        def _sync_checkpoint() -> tuple[int, int, int]:
            with sqlite3.connect(db_path) as conn:
                row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE);").fetchone()
            if row is None:
                return (1, 0, 0)
            return int(row[0]), int(row[1]), int(row[2])

        busy, log_frames, checkpointed = await asyncio.to_thread(_sync_checkpoint)
        return {
            "checkpointed": busy == 0,
            "busy": busy,
            "log_frames": log_frames,
            "checkpointed_frames": checkpointed,
        }

    async def _recover_db_to_new_file(
        self,
        source_db_relative_path: str,
        output_relative_path: str,
    ) -> dict[str, Any]:
        source = self._path_guard.resolve_in_sandbox(source_db_relative_path)
        output = self._path_guard.resolve_in_sandbox(output_relative_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        def _sync_recover() -> None:
            with sqlite3.connect(source) as src, sqlite3.connect(output) as dst:
                src.backup(dst)

        await asyncio.to_thread(_sync_recover)
        return {"recovered": True, "output_path": output.as_posix()}

    async def _move_path(
        self,
        source_relative_path: str,
        destination_relative_path: str,
        overwrite: bool,
    ) -> dict[str, Any]:
        source = self._path_guard.resolve_in_sandbox(source_relative_path)
        destination = self._path_guard.resolve_in_sandbox(destination_relative_path)
        if not source.exists() and not source.is_symlink():
            raise ToolParameterValidationError(f"Source path not found: {source_relative_path}")

        if destination.exists() or destination.is_symlink():
            if not overwrite:
                raise ToolParameterValidationError(
                    f"Destination already exists: {destination_relative_path}"
                )
            self._remove_existing_path(destination)

        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        return {"moved": True, "destination_path": destination.as_posix()}

    async def _write_app_file(self, relative_path: str, content: str) -> dict[str, Any]:
        target = self._path_guard.resolve_app_file(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"written": True, "bytes": len(content.encode("utf-8"))}

    async def _remove_path(self, relative_path: str, recursive: bool) -> dict[str, Any]:
        target = self._path_guard.resolve_in_sandbox(relative_path)
        if not target.exists() and not target.is_symlink():
            return {"removed": False}

        if target.is_symlink() or target.is_file():
            target.unlink()
            return {"removed": True}

        if target.is_dir():
            if recursive:
                rmtree(target)
            else:
                target.rmdir()
            return {"removed": True}

        raise ToolParameterValidationError(f"Unsupported path type: {relative_path}")

    @staticmethod
    def _extract_expected_tables(schema_sql: str) -> set[str]:
        tables: set[str] = set()
        for line in schema_sql.splitlines():
            normalized = line.strip().lower()
            if normalized.startswith("create table"):
                parts = line.replace("(", " ").split()
                if len(parts) >= 3:
                    tables.add(parts[2].strip('"`'))
        return tables

    async def _create_directory(self, relative_path: str) -> dict[str, Any]:
        target = self._path_guard.resolve_in_sandbox(relative_path)
        target.mkdir(parents=True, exist_ok=True)
        return {"created": True, "path": str(target)}

    async def _create_empty_sqlite_db(self, relative_path: str) -> dict[str, Any]:
        db_path = self._path_guard.resolve_in_sandbox(relative_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        def _sync_create() -> None:
            with sqlite3.connect(db_path):
                pass

        await asyncio.to_thread(_sync_create)
        return {"created": True, "path": str(db_path)}

    async def _apply_schema(
        self, db_relative_path: str, schema_relative_path: str
    ) -> dict[str, Any]:
        db_path = self._path_guard.resolve_in_sandbox(db_relative_path)
        schema_path = self._path_guard.resolve_app_file(schema_relative_path)
        sql = schema_path.read_text(encoding="utf-8")

        def _sync_apply() -> None:
            with sqlite3.connect(db_path) as conn:
                conn.executescript(sql)

        await asyncio.to_thread(_sync_apply)
        return {"applied": True, "db_path": str(db_path)}

    async def _set_permissions(self, relative_path: str, mode: int) -> dict[str, Any]:
        target = self._path_guard.resolve_in_sandbox(relative_path)
        db_dir = self._settings.db_file.parent.resolve(strict=False)
        target_resolved = target.resolve(strict=False)
        if target_resolved != db_dir and target_resolved != self._settings.db_file.resolve(
            strict=False
        ):
            raise ToolParameterValidationError(
                "set_permissions only allowed for DB directory or DB file"
            )
        target.chmod(mode)
        return {"changed": True, "mode": oct(mode)}

    async def _restart_service(self, service_name: str) -> dict[str, Any]:
        if self._restart_service_hook is not None:
            await self._restart_service_hook(service_name)
        return {"restarted": True, "service": service_name}
