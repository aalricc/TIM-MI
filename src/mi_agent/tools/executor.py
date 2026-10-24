from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from mi_agent.config import MISettings
from mi_agent.errors import SecurityGateError, ToolExecutionError
from mi_agent.sqlite_docs_tool import search_sqlite_docs
from mi_agent.tools.pathing import PathGuard
from mi_agent.tools.schemas import (
    ReplaceFileParams,
    RequestBackupRestoreParams,
    RestartServiceParams,
    RunSQLParams,
)
from mi_agent.workflow.models import DynamicToolSpec, MonitorStatus, RAGChunk

RestoreHook = Callable[[str], Awaitable[dict[str, Any]]]


def base_tool_name(spec: DynamicToolSpec) -> str:
    """Session tool name for a spec before de-duplication suffixes; also the RL policy key."""
    return spec.name.strip().replace(" ", "_") or "dynamic_tool"

RestartHook = Callable[[str], Awaitable[dict[str, Any]]]


class ToolExecutor:
    def __init__(
        self,
        settings: MISettings,
        *,
        request_backup_restore_hook: RestoreHook | None = None,
        restart_service_hook: RestartHook | None = None,
    ) -> None:
        self._logger = logging.getLogger(__name__)
        self._settings = settings
        self._paths = PathGuard(settings)
        self._request_backup_restore_hook = request_backup_restore_hook
        self._restart_service_hook = restart_service_hook

        self._catalog_path = settings.tool_catalog_path
        self._catalog: dict[str, dict[str, Any]] = self._load_catalog()
        self._session_tools: dict[str, dict[str, DynamicToolSpec]] = {}
        self._seen_log_fingerprints: set[str] = set()
        self._last_rag_meta: dict[str, Any] = {}

    @property
    def catalog(self) -> dict[str, dict[str, Any]]:
        return dict(self._catalog)

    async def monitor_health(self) -> MonitorStatus:
        health = await self._get_json(self._settings.monitor_health_path)
        new_log_errors = await self._new_error_log_entries(lines=200)

        health_ok = bool(health.get("ok", False))
        if not health_ok:
            detail = "health_endpoint_unhealthy"
        elif new_log_errors:
            detail = "error_log_detected"
        else:
            detail = "healthy"

        payload = {
            "health": health,
            "new_log_errors": new_log_errors,
        }
        return MonitorStatus(
            healthy=health_ok,
            status_code=int(health.get("status_code", 0) or 0),
            detail=detail,
            payload=payload,
        )

    async def collect_diagnostics(self) -> dict[str, Any]:
        health_deep = await self._get_json(self._settings.monitor_health_path)
        db_status = await self._get_json(self._settings.db_status_endpoint_path)
        log_payload = await self._read_logs_via_endpoint(lines=300)
        raw_lines = log_payload.get("lines")
        endpoint_log_tail = [str(line) for line in raw_lines] if isinstance(raw_lines, list) else []
        file_log_tail = self.read_app_log(lines=200)
        log_tail = endpoint_log_tail if endpoint_log_tail else file_log_tail
        backups = await self.list_backups()
        db_integrity = self.check_db_integrity(self._settings.db_file)
        runtime_paths = {
            "db_file": self._settings.db_file.as_posix(),
            "db_exists": self._settings.db_file.exists(),
            "schema_file": self._settings.schema_file.as_posix(),
            "schema_exists": self._settings.schema_file.exists(),
            "app_log": self._settings.app_log.as_posix(),
            "app_log_exists": self._settings.app_log.exists(),
        }
        return {
            "health_deep": health_deep,
            "db_status": db_status,
            "db_integrity": db_integrity,
            "runtime_paths": runtime_paths,
            "log_tail": log_tail,
            "log_endpoint": log_payload,
            "backups": backups,
        }

    async def list_backups(self) -> list[dict[str, Any]]:
        """The target's backup catalog (newest first); empty if the target has none."""
        payload = await self._get_json(self._settings.backups_endpoint_path)
        rows = payload.get("backups")
        if not isinstance(rows, list):
            return []
        return [dict(row) for row in rows if isinstance(row, dict)]

    async def retrieve_rag_chunks(self, query: str, *, k: int) -> list[RAGChunk]:
        query_used = str(query)[:1200]

        if not self._settings.rag_enabled:
            self._last_rag_meta = {
                "status": "skipped",
                "reason": "rag_disabled",
                "query_used": query_used,
                "count": 0,
            }
            self._logger.warning("RAG retrieval skipped: rag disabled")
            return []

        api_key = self._settings.pinecone_api_key
        host = self._settings.pinecone_index_host
        if api_key is None or host is None:
            self._last_rag_meta = {
                "status": "skipped",
                "reason": "missing_config",
                "query_used": query_used,
                "count": 0,
            }
            self._logger.warning("RAG retrieval skipped: missing Pinecone configuration")
            return []

        try:
            rows = await self._run_in_thread(
                search_sqlite_docs,
                query,
                k,
                api_key=api_key,
                index_host=host,
                namespace=self._settings.pinecone_namespace,
            )
            chunks = [RAGChunk.model_validate(row) for row in rows]
        except Exception as exc:
            self._last_rag_meta = {
                "status": "error",
                "reason": "search_failed",
                "query_used": query_used,
                "count": 0,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            }
            self._logger.warning(
                "RAG retrieval failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            return []

        status = "ok" if chunks else "empty"
        self._last_rag_meta = {
            "status": status,
            "reason": "search_ok",
            "query_used": query_used,
            "count": len(chunks),
        }
        if status == "empty":
            self._logger.warning("RAG retrieval returned no hits")
        return chunks

    def consume_last_rag_meta(self) -> dict[str, Any]:
        payload = dict(self._last_rag_meta)
        self._last_rag_meta = {}
        return payload

    def register_temporary_dynamic_tool(self, incident_id: str, spec: DynamicToolSpec) -> str:
        namespace = self._session_tools.setdefault(incident_id, {})
        candidate_name = base_tool_name(spec)

        if candidate_name not in namespace:
            namespace[candidate_name] = spec
            return candidate_name

        suffix = 2
        while True:
            alternate = f"{candidate_name}_{suffix}"
            if alternate not in namespace:
                namespace[alternate] = spec.model_copy(update={"name": alternate})
                return alternate
            suffix += 1

    async def execute_dynamic_tool(self, incident_id: str, tool_name: str) -> dict[str, Any]:
        spec = self._session_tools.get(incident_id, {}).get(tool_name)
        if spec is None:
            raise ToolExecutionError(f"dynamic tool not found: {tool_name}")
        # Re-checked at execution time so no caller can execute an ungated spec.
        self.security_gate(spec)
        return await self._execute_operation(spec.operation, spec.params)

    def discard_incident_tools(self, incident_id: str, *, keep: str | None = None) -> None:
        namespace = self._session_tools.get(incident_id)
        if namespace is None:
            return
        if keep is None:
            namespace.clear()
            return
        kept = namespace.get(keep)
        namespace.clear()
        if kept is not None:
            namespace[keep] = kept

    def persist_dynamic_tool(self, incident_id: str, tool_name: str) -> None:
        spec = self._session_tools.get(incident_id, {}).get(tool_name)
        if spec is None:
            raise ToolExecutionError(f"cannot persist missing dynamic tool: {tool_name}")
        self._catalog[tool_name] = {
            "name": spec.name,
            "rationale": spec.rationale,
            "operation": spec.operation,
            "params": spec.params,
        }
        self._catalog_path.parent.mkdir(parents=True, exist_ok=True)
        self._catalog_path.write_text(json.dumps(self._catalog, indent=2), encoding="utf-8")

    def read_app_log(self, *, lines: int) -> list[str]:
        if not self._settings.app_log.exists():
            return []
        all_lines = self._settings.app_log.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()
        return all_lines[-lines:]

    def check_db_integrity(self, db_path: Path) -> dict[str, Any]:
        if not db_path.exists():
            return {"ok": False, "error": "db_missing"}
        try:
            with sqlite3.connect(db_path) as connection:
                result = connection.execute("PRAGMA integrity_check;").fetchall()
        except sqlite3.Error as exc:
            return {"ok": False, "error": str(exc)}
        rows = [str(item[0]) for item in result]
        return {"ok": all(row.lower() == "ok" for row in rows), "rows": rows}

    def state_fingerprint(self) -> str:
        payload = {
            "db_exists": self._settings.db_file.exists(),
            "db_size": (
                self._settings.db_file.stat().st_size
                if self._settings.db_file.exists()
                else 0
            ),
            "schema_exists": self._settings.schema_file.exists(),
            "log_exists": self._settings.app_log.exists(),
        }
        blob = json.dumps(payload, sort_keys=True).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def security_gate(self, spec: DynamicToolSpec) -> None:
        """README step 9: schema/path/sandbox checks. Raises on rejection."""
        allowed_operations = {
            "request_backup_restore",
            "restart_service",
            "run_sql",
            "replace_file",
        }
        if spec.operation not in allowed_operations:
            raise SecurityGateError(f"operation not allowed by security gate: {spec.operation}")
        if spec.operation == "run_sql":
            RunSQLParams.model_validate(spec.params)
        elif spec.operation == "replace_file":
            parsed = ReplaceFileParams.model_validate(spec.params)
            self._paths.resolve_in_sandbox(parsed.source_path)
            self._paths.resolve_in_app_root(parsed.destination_path)
        elif spec.operation == "request_backup_restore":
            RequestBackupRestoreParams.model_validate(spec.params)
        elif spec.operation == "restart_service":
            RestartServiceParams.model_validate(spec.params)

    async def _execute_operation(self, operation: str, params: dict[str, Any]) -> dict[str, Any]:
        if operation == "request_backup_restore":
            parsed_restore = RequestBackupRestoreParams.model_validate(params)
            return await self._request_backup_restore(parsed_restore.backup_id)
        if operation == "restart_service":
            parsed_restart = RestartServiceParams.model_validate(params)
            return await self._restart_service(parsed_restart.service_name)
        if operation == "run_sql":
            parsed_sql = RunSQLParams.model_validate(params)
            return await self._run_sql(parsed_sql)
        if operation == "replace_file":
            parsed_replace = ReplaceFileParams.model_validate(params)
            return await self._replace_file(parsed_replace)
        raise ToolExecutionError(f"unknown operation: {operation}")

    async def _request_backup_restore(self, backup_id: str) -> dict[str, Any]:
        if self._request_backup_restore_hook is not None:
            return await self._request_backup_restore_hook(backup_id)

        root = self._settings.backups_endpoint_path.rstrip("/")
        path = f"{root}/{quote(backup_id, safe='')}/restore"
        payload = await self._post_json(path)
        if bool(payload.get("ok", False)):
            return {"ok": True, "action": "request_backup_restore", "backup_id": backup_id}

        detail = payload.get("detail")
        reason = detail if isinstance(detail, dict) else payload
        error = str(reason.get("error") or f"restore_failed:{payload.get('status_code')}")
        available = reason.get("available")
        if error == "backup_not_found" and isinstance(available, list):
            # The ids go into the error text because that is what the retry context keeps.
            ids = ", ".join(str(item) for item in available) or "none"
            error = f"backup_not_found: {backup_id!r}; available: {ids}"
        return {
            "ok": False,
            "action": "request_backup_restore",
            "backup_id": backup_id,
            "error": error,
        }

    async def _restart_service(self, service_name: str) -> dict[str, Any]:
        if self._restart_service_hook is not None:
            return await self._restart_service_hook(service_name)
        return {
            "ok": False,
            "action": "restart_service",
            "service_name": service_name,
            "error": "no_restart_hook_configured",
        }

    async def _run_sql(self, params: RunSQLParams) -> dict[str, Any]:
        db_path = self._paths.resolve_in_app_root(params.db_path)
        if not db_path.exists():
            return {"ok": False, "error": "db_missing", "db_path": db_path.as_posix()}

        def _execute() -> None:
            with sqlite3.connect(db_path) as connection:
                connection.executescript(params.sql)
                connection.commit()

        try:
            await self._run_in_thread(_execute)
        except sqlite3.Error as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "action": "run_sql", "db_path": db_path.as_posix()}

    async def _replace_file(self, params: ReplaceFileParams) -> dict[str, Any]:
        source = self._paths.resolve_in_sandbox(params.source_path)
        destination = self._paths.resolve_in_app_root(params.destination_path)
        if not source.exists():
            return {"ok": False, "error": "source_missing", "source": source.as_posix()}

        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return {
            "ok": True,
            "action": "replace_file",
            "source": source.as_posix(),
            "destination": destination.as_posix(),
        }

    def _load_catalog(self) -> dict[str, dict[str, Any]]:
        if not self._catalog_path.exists():
            return {}
        try:
            raw = json.loads(self._catalog_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(raw, dict):
            return {}
        valid: dict[str, dict[str, Any]] = {}
        for key, value in raw.items():
            if isinstance(key, str) and isinstance(value, dict):
                valid[key] = value
        return valid

    async def _get_json(self, path: str) -> dict[str, Any]:
        url = self._join_url(path)
        async with httpx.AsyncClient(timeout=self._settings.request_timeout_seconds) as client:
            try:
                response = await client.get(url)
            except httpx.HTTPError as exc:
                return {"ok": False, "error": str(exc)}
        payload = self._json_or_default(response)
        if "ok" not in payload:
            payload["ok"] = response.status_code < 400
        payload["status_code"] = response.status_code
        return payload

    async def _post_json(self, path: str) -> dict[str, Any]:
        url = self._join_url(path)
        async with httpx.AsyncClient(timeout=self._settings.request_timeout_seconds) as client:
            try:
                response = await client.post(url)
            except httpx.HTTPError as exc:
                return {"ok": False, "error": str(exc)}
        payload = self._json_or_default(response)
        if "ok" not in payload:
            payload["ok"] = response.status_code < 400
        payload["status_code"] = response.status_code
        return payload

    async def _read_logs_via_endpoint(self, *, lines: int) -> dict[str, Any]:
        safe_lines = max(1, min(lines, 5000))
        return await self._get_json(f"{self._settings.logs_endpoint_path}?lines={safe_lines}")

    async def _new_error_log_entries(self, *, lines: int) -> list[str]:
        payload = await self._read_logs_via_endpoint(lines=lines)
        rows = payload.get("lines")
        if not isinstance(rows, list):
            return []

        errors: list[str] = []
        for row in rows:
            row_text = str(row)
            fingerprint = hashlib.sha256(row_text.encode("utf-8")).hexdigest()
            if fingerprint in self._seen_log_fingerprints:
                continue
            self._seen_log_fingerprints.add(fingerprint)

            try:
                parsed = json.loads(row_text)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            level = str(parsed.get("level", "")).lower()
            if level in {"error", "critical"}:
                message = str(parsed.get("message", "error log"))
                errors.append(message)

        if len(self._seen_log_fingerprints) > 10_000:
            self._seen_log_fingerprints.clear()

        return errors

    @staticmethod
    async def _run_in_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        import asyncio

        return await asyncio.to_thread(func, *args, **kwargs)

    @staticmethod
    def _json_or_default(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            return {"ok": False, "raw": response.text}
        if isinstance(payload, dict):
            return {str(key): value for key, value in payload.items()}
        return {"ok": response.status_code < 400, "payload": payload}

    def _join_url(self, path: str) -> str:
        root = self._settings.target_service_url.rstrip("/")
        suffix = path if path.startswith("/") else f"/{path}"
        return f"{root}{suffix}"
