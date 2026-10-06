from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from shutil import copy2
from stat import S_IMODE
from types import MappingProxyType
from typing import Any, cast
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from mi_agent.config import MISettings
from mi_agent.errors import PrimitiveNotAllowedError, ToolParameterValidationError
from mi_agent.events.bus import EventBus
from mi_agent.events.models import Event
from mi_agent.tools.pathing import PathGuard
from mi_agent.tools.schemas import (
    ApplySchemaParams,
    CreateDirectoryParams,
    CreateEmptySQLiteDBParams,
    EmptyParams,
    ListDirectoryParams,
    ReadAppFileParams,
    ReadAppLogParams,
    RestartServiceParams,
    SetPermissionsParams,
    StatPathParams,
)


@dataclass(frozen=True, slots=True)
class PrimitiveSpec:
    primitive_id: str
    params_model: type[BaseModel]
    mutating: bool
    reversible: bool


@dataclass(frozen=True, slots=True)
class ToolSnapshot:
    snapshot_id: str
    db_existed: bool
    db_backup_path: Path | None


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    primitive_id: str
    data: dict[str, Any]
    snapshot: ToolSnapshot | None


class ToolExecutor:
    def __init__(
        self,
        settings: MISettings,
        event_bus: EventBus,
        restart_service_hook: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._settings = settings
        self._event_bus = event_bus
        self._path_guard = PathGuard(settings)
        self._restart_service_hook = restart_service_hook
        self._snapshot_dir = settings.state_dir / "snapshots"
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._specs = MappingProxyType(
            {
                "ping_db": PrimitiveSpec("ping_db", EmptyParams, False, False),
                "read_app_log": PrimitiveSpec("read_app_log", ReadAppLogParams, False, False),
                "list_directory": PrimitiveSpec(
                    "list_directory", ListDirectoryParams, False, False
                ),
                "stat_path": PrimitiveSpec("stat_path", StatPathParams, False, False),
                "read_app_file": PrimitiveSpec("read_app_file", ReadAppFileParams, False, False),
                "check_schema": PrimitiveSpec("check_schema", EmptyParams, False, False),
                "create_directory": PrimitiveSpec(
                    "create_directory", CreateDirectoryParams, True, False
                ),
                "create_empty_sqlite_db": PrimitiveSpec(
                    "create_empty_sqlite_db", CreateEmptySQLiteDBParams, True, True
                ),
                "apply_schema": PrimitiveSpec("apply_schema", ApplySchemaParams, True, True),
                "set_permissions": PrimitiveSpec(
                    "set_permissions", SetPermissionsParams, True, True
                ),
                "restart_service": PrimitiveSpec(
                    "restart_service", RestartServiceParams, True, False
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
        snapshot = await self._pre_action_snapshot(spec)
        data = await self._run_primitive(spec.primitive_id, validated_params)
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
        db_path = self._settings.db_file
        if snapshot.db_existed:
            if snapshot.db_backup_path is not None and snapshot.db_backup_path.exists():
                db_path.parent.mkdir(parents=True, exist_ok=True)
                copy2(snapshot.db_backup_path, db_path)
        elif db_path.exists():
            db_path.unlink()
        await self._event_bus.publish(
            Event(event_type="tool.rollback", payload={"snapshot_id": snapshot.snapshot_id})
        )

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
            if primitive_id == "read_app_file":
                self._path_guard.resolve_app_file(path_value)
            else:
                self._path_guard.resolve_in_sandbox(path_value)
        db_path = data.get("db_path")
        if isinstance(db_path, str):
            self._path_guard.resolve_in_sandbox(db_path)
        schema_path = data.get("schema_path")
        if isinstance(schema_path, str):
            self._path_guard.resolve_app_file(schema_path)

    async def _pre_action_snapshot(self, spec: PrimitiveSpec) -> ToolSnapshot | None:
        if not spec.mutating:
            return None
        db_path = self._settings.db_file
        existed = db_path.exists()
        backup_path: Path | None = None
        if existed:
            snapshot_id = str(uuid4())
            backup_path = self._snapshot_dir / f"{snapshot_id}.db"
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            copy2(db_path, backup_path)
        else:
            snapshot_id = str(uuid4())
        return ToolSnapshot(snapshot_id=snapshot_id, db_existed=existed, db_backup_path=backup_path)

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
