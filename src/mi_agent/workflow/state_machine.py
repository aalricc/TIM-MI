from __future__ import annotations

from enum import StrEnum


class WorkflowState(StrEnum):
    HEALTHY = "healthy"
    INCIDENT_ACTIVE = "incident_active"
    RECOVERED = "recovered"


class WorkflowEvent(StrEnum):
    UNHEALTHY_DETECTED = "unhealthy_detected"
    RECOVERY_FAILED = "recovery_failed"
    RECOVERY_SUCCEEDED = "recovery_succeeded"
    STABLE = "stable"


def transition(state: WorkflowState, event: WorkflowEvent) -> WorkflowState:
    if state == WorkflowState.HEALTHY:
        if event == WorkflowEvent.UNHEALTHY_DETECTED:
            return WorkflowState.INCIDENT_ACTIVE
        return WorkflowState.HEALTHY
    if state == WorkflowState.INCIDENT_ACTIVE:
        if event == WorkflowEvent.RECOVERY_SUCCEEDED:
            return WorkflowState.RECOVERED
        return WorkflowState.INCIDENT_ACTIVE
    if event == WorkflowEvent.UNHEALTHY_DETECTED:
        return WorkflowState.INCIDENT_ACTIVE
    return WorkflowState.HEALTHY
