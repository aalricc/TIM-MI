from __future__ import annotations

import uvicorn

from mi_agent.api.server import create_api
from mi_agent.config import MISettings
from mi_agent.runtime import build_agent


def run() -> None:
    settings = MISettings()
    agent, context = build_agent(settings)
    app = create_api(context, agent)
    uvicorn.run(app, host="0.0.0.0", port=8090)


if __name__ == "__main__":
    run()
