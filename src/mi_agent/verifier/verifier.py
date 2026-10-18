from __future__ import annotations

from typing import Any

import httpx

from mi_agent.config import MISettings
from mi_agent.workflow.models import VerificationResult


class Verifier:
    def __init__(self, settings: MISettings) -> None:
        self._settings = settings

    async def verify(self) -> VerificationResult:
        url = f"{self._settings.target_service_url.rstrip('/')}{self._settings.monitor_health_path}"
        async with httpx.AsyncClient(timeout=self._settings.request_timeout_seconds) as client:
            try:
                response = await client.get(url)
            except httpx.HTTPError as exc:
                return VerificationResult(
                    healthy=False,
                    failure_type="verifier_unreachable",
                    details={"error": str(exc)},
                )

        payload = self._response_payload(response)
        healthy = response.status_code < 400 and bool(payload.get("ok", response.status_code < 400))
        return VerificationResult(
            healthy=healthy,
            failure_type=None if healthy else str(payload.get("error") or "unhealthy"),
            details={"status_code": response.status_code, "payload": payload},
        )

    @staticmethod
    def _response_payload(response: httpx.Response) -> dict[str, Any]:
        try:
            parsed = response.json()
        except ValueError:
            return {"raw": response.text}
        if isinstance(parsed, dict):
            return {str(key): value for key, value in parsed.items()}
        return {"payload": parsed}
