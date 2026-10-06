from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from mi_agent.config import MISettings
from mi_agent.failure_types import UNCLASSIFIED_FAILURE
from mi_agent.llm.client import LLMClient
from mi_agent.policy.store import PolicyStore
from mi_agent.tools.executor import ToolExecutor
from mi_agent.workflow.models import Diagnosis

_MAX_LOG_LINES = 100
_MAX_LINE_LEN = 300
def sanitize_lines(lines: Sequence[str]) -> list[str]:
    sanitized: list[str] = []
    for line in lines[-_MAX_LOG_LINES:]:
        stripped = " ".join(line.strip().split())
        sanitized.append(stripped[:_MAX_LINE_LEN])
    return sanitized


def probe_fingerprint(probes: dict[str, Any], logs: Sequence[str]) -> dict[str, str]:
    compact = json.dumps(probes, sort_keys=True, default=str)
    log_token = "|".join(sanitize_lines(logs)[-3:])
    return {
        "probes": compact[:300],
        "logs": log_token[:300],
    }


class Diagnoser:
    def __init__(
        self,
        llm_client: LLMClient,
        tool_executor: ToolExecutor,
        policy_store: PolicyStore,
        settings: MISettings,
    ) -> None:
        self._llm = llm_client
        self._tools = tool_executor
        self._policy_store = policy_store
        self._settings = settings

    async def collect_logs(self) -> list[str]:
        result = await self._tools.execute("read_app_log", {"lines": _MAX_LOG_LINES})
        payload = result.data.get("lines", [])
        if isinstance(payload, list):
            return [str(item) for item in payload]
        return []

    async def collect_probes(self) -> dict[str, Any]:
        db_path = self._relative_path(self._settings.db_file, self._settings.sandbox_root)
        schema_path = self._relative_path(self._settings.schema_file, self._settings.sandbox_root)

        ping_result = await self._tools.execute("ping_db", {})
        db_stat = await self._tools.execute("stat_path", {"path": db_path})
        schema_stat = await self._tools.execute("stat_path", {"path": schema_path})
        schema_check = await self._tools.execute("check_schema", {})

        return {
            "db_ping": bool(ping_result.data.get("ok", False)),
            "db_file": db_stat.data,
            "schema_file": schema_stat.data,
            "schema_ok": bool(schema_check.data.get("schema_ok", False)),
        }

    async def diagnose(
        self,
        *,
        failure_type_hint: str | None,
        recent_logs: Sequence[str],
        probes: dict[str, Any] | None = None,
    ) -> tuple[Diagnosis, dict[str, Any], dict[str, str]]:
        effective_probes = probes if probes is not None else await self.collect_probes()
        lessons: list[str] = []
        if failure_type_hint is not None and failure_type_hint != UNCLASSIFIED_FAILURE:
            lessons = self._policy_store.recall_lessons(failure_type_hint)

        diagnosis = await self._llm.diagnose(
            failure_type_hint=failure_type_hint,
            sanitized_logs=sanitize_lines(recent_logs),
            probes=effective_probes,
            lessons=lessons,
        )
        fingerprint = probe_fingerprint(effective_probes, recent_logs)
        return diagnosis, effective_probes, fingerprint

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
