from __future__ import annotations

from mi_agent.agent.agent import MIAgent, MIContext
from mi_agent.agent.selector import EpsilonGreedySelector
from mi_agent.alerts.sink import ConsoleAlertSink
from mi_agent.config import MISettings
from mi_agent.events.bus import EventBus
from mi_agent.failure_taxonomy import FailureTaxonomy
from mi_agent.llm.client import LLMClient, OpenAILLMClient
from mi_agent.policy.decision_log import DecisionLogStore
from mi_agent.policy.store import PolicyStore
from mi_agent.skills.distiller import SkillDistiller
from mi_agent.skills.embedding import DeterministicEmbedder
from mi_agent.skills.library import SkillLibrary
from mi_agent.skills.matcher import SkillMatcher
from mi_agent.supervisor.supervisor import Supervisor
from mi_agent.tools.executor import ToolExecutor
from mi_agent.verifier.verifier import Verifier


def build_agent(
    settings: MISettings, llm_client: LLMClient | None = None
) -> tuple[MIAgent, MIContext]:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    event_bus = EventBus(settings.event_log_path)
    policy = PolicyStore(
        settings.policy_store_path,
        recency_alpha=settings.policy_recency_alpha,
    )
    decision_log = DecisionLogStore(settings.decision_log_path)
    failure_taxonomy = FailureTaxonomy(
        settings.failure_taxonomy_path,
        merge_threshold=settings.taxonomy_merge_threshold,
        review_threshold=settings.taxonomy_review_threshold,
    )
    skills = SkillLibrary(
        settings.skill_store_path,
        candidate_to_canary_successes=settings.skill_candidate_to_canary_successes,
        canary_to_trusted_successes=settings.skill_canary_to_trusted_successes,
        trusted_regression_failures=settings.skill_trusted_regression_failures,
    )
    llm = llm_client or OpenAILLMClient(settings)
    supervisor = Supervisor(settings, event_bus)
    tools = ToolExecutor(
        settings,
        event_bus,
        request_backup_restore_hook=supervisor.request_backup_restore,
    )
    verifier = Verifier(settings, event_bus)
    matcher = SkillMatcher(
        skills,
        llm,
        embedder=DeterministicEmbedder(),
        similarity_threshold=settings.skill_similarity_threshold,
    )
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
        policy_version=settings.policy_version,
        decision_log=decision_log,
        failure_taxonomy=failure_taxonomy,
        skill_library=skills,
        llm=llm,
        selector=selector,
        skill_matcher=matcher,
        skill_distiller=distiller,
        alerts=alerts,
    )
    return MIAgent(context), context
