from __future__ import annotations

import uvicorn

from mi_agent.api.server import create_api
from mi_agent.config import MISettings
from mi_agent.runtime import build_agent


def run() -> None:
    settings = MISettings()  # type: ignore[call-arg]
    agent, context = build_agent(settings)
    app = create_api(context, agent)
    # Listens on all interfaces so the API is reachable from outside the container.
    uvicorn.run(app, host="0.0.0.0", port=8090)  # noqa: S104


if __name__ == "__main__":
    run()
