from __future__ import annotations

from dataclasses import dataclass

from mi_agent.agent.engine import RecoveryAgent
from mi_agent.class_memory import ClassMemory, ClassMemoryConfig
from mi_agent.config import MISettings
from mi_agent.decision.client import JevDecisionClient
from mi_agent.events import EventLog
from mi_agent.llm.client import LLMClient, OpenAILLMClient
from mi_agent.tools.executor import ToolExecutor
from mi_agent.verifier.verifier import Verifier


@dataclass(slots=True)
class MIContext:
    settings: MISettings
    events: EventLog
    tools: ToolExecutor
    verifier: Verifier
    llm: LLMClient


def build_agent(
    settings: MISettings,
    llm_client: LLMClient | None = None,
) -> tuple[RecoveryAgent, MIContext]:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    events = EventLog(settings.event_log_path)
    llm = llm_client or OpenAILLMClient(settings)
    tools = ToolExecutor(settings)
    verifier = Verifier(settings)
    jev = JevDecisionClient(settings)
    class_memory = None
    if settings.classmem_enabled:
        class_memory = ClassMemory(
            ClassMemoryConfig(
                store_path=settings.classmem_store_path,
                join_threshold=settings.classmem_join_threshold,
                margin_delta=settings.classmem_margin_delta,
                tau_conf=settings.classmem_tau_conf,
                n_min=settings.classmem_n_min,
                cooldown_seconds=settings.classmem_cooldown_seconds,
                embedding_dim=settings.classmem_embedding_dim,
                min_similarity_for_id=0.2,
            )
        )
    agent = RecoveryAgent(
        llm=llm,
        tools=tools,
        verifier=verifier,
        events=events,
        policy_store_path=settings.policy_store_path.as_posix(),
        monitor_interval_seconds=settings.monitor_interval_seconds,
        jev=jev,
        class_memory=class_memory,
        classmem_enabled=settings.classmem_enabled,
        classmem_dedupe_enabled=settings.classmem_dedupe_enabled,
        classmem_fastpath_enabled=settings.classmem_fastpath_enabled,
    )
    context = MIContext(
        settings=settings,
        events=events,
        tools=tools,
        verifier=verifier,
        llm=llm,
    )
    return agent, context
