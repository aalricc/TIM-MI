from __future__ import annotations

from mi_agent.agent.agent import MIAgent, MIContext
from mi_agent.agent.selector import EpsilonGreedySelector
from mi_agent.alerts.sink import ConsoleAlertSink
from mi_agent.config import MISettings
from mi_agent.events.bus import EventBus
from mi_agent.llm.client import LLMClient, OpenAILLMClient
from mi_agent.policy.store import PolicyStore
from mi_agent.skills.distiller import SkillDistiller
from mi_agent.skills.library import SkillLibrary
from mi_agent.skills.matcher import SkillMatcher
from mi_agent.supervisor.last_resort import HTTPLastResortClient, LocalLastResortClient
from mi_agent.supervisor.supervisor import Supervisor
from mi_agent.tools.executor import ToolExecutor
from mi_agent.verifier.verifier import Verifier


def build_agent(
    settings: MISettings, llm_client: LLMClient | None = None
) -> tuple[MIAgent, MIContext]:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    event_bus = EventBus()
    tools = ToolExecutor(settings, event_bus)
    policy = PolicyStore(settings.policy_store_path)
    skills = SkillLibrary(settings.skill_store_path)
    llm = llm_client or OpenAILLMClient(settings)
    restorer = (
        HTTPLastResortClient(settings.last_resort_url)
        if settings.last_resort_url
        else LocalLastResortClient(settings.backups_root, settings.db_file)
    )
    supervisor = Supervisor(settings, event_bus, restorer)
    verifier = Verifier(settings, event_bus)
    matcher = SkillMatcher(skills, llm)
    distiller = SkillDistiller(llm, tools)
    selector = EpsilonGreedySelector(settings)
    alerts = ConsoleAlertSink()

    context = MIContext(
        settings=settings,
        events=event_bus,
        supervisor=supervisor,
        verifier=verifier,
        tools=tools,
        policy=policy,
        skill_library=skills,
        llm=llm,
        selector=selector,
        skill_matcher=matcher,
        skill_distiller=distiller,
        alerts=alerts,
    )
    return MIAgent(context), context
