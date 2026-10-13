from __future__ import annotations

import pytest

from mi_agent.errors import ToolExecutionError
from mi_agent.events.bus import EventBus
from mi_agent.tools.executor import ToolExecutor

pytestmark = pytest.mark.phase0


@pytest.mark.asyncio()
async def test_apply_schema_current_behavior_is_not_idempotent(
    test_settings,
) -> None:
    tools = ToolExecutor(test_settings, EventBus())

    test_settings.db_file.unlink()
    await tools.execute("create_empty_sqlite_db", {"path": "data/app.db"})

    result = await tools.execute("apply_schema", {})
    assert result.data["applied"] is True

    with pytest.raises(ToolExecutionError):
        await tools.execute("apply_schema", {})


@pytest.mark.asyncio()
async def test_restart_service_without_hook_is_a_noop_success_result(test_settings) -> None:
    tools = ToolExecutor(test_settings, EventBus())

    result = await tools.execute("restart_service", {})

    assert result.data == {"restarted": True, "service": "target-app"}


@pytest.mark.asyncio()
async def test_restart_service_with_hook_calls_hook_once(test_settings) -> None:
    calls: list[str] = []

    async def _hook(service_name: str) -> None:
        calls.append(service_name)

    tools = ToolExecutor(test_settings, EventBus(), restart_service_hook=_hook)
    result = await tools.execute("restart_service", {"service_name": "target-app"})

    assert calls == ["target-app"]
    assert result.data["restarted"] is True
