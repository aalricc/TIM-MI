from __future__ import annotations

from enum import StrEnum


class WorkflowState(StrEnum):
    START = "start"
    PING_DB = "ping_db"
    CHECK_LOGS = "check_logs"
    OPEN_INCIDENT = "open_incident"
    CHECK_CAP = "check_cap"
    DIAGNOSE = "diagnose"
    CHOOSE_ACTION = "choose_action"
    VALIDATE_ACTION = "validate_action"
    EXECUTE = "execute"
    VERIFY = "verify"
    UPDATE_AND_LEARN = "update_and_learn"
    RESOLVED = "resolved"
    CAP_REACHED = "cap_reached"


class WorkflowEvent(StrEnum):
    PING_OK = "ping_ok"
    PING_FAIL = "ping_fail"
    LOGS_COLLECTED = "logs_collected"
    INCIDENT_OPENED = "incident_opened"
    CAP_AVAILABLE = "cap_available"
    CAP_EXCEEDED = "cap_exceeded"
    DIAGNOSED = "diagnosed"
    ACTION_CHOSEN = "action_chosen"
    ACTION_VALID = "action_valid"
    ACTION_EXECUTED = "action_executed"
    VERIFIED_HEALTHY = "verified_healthy"
    VERIFIED_UNHEALTHY = "verified_unhealthy"
    LEARNED = "learned"


_TRANSITIONS: dict[tuple[WorkflowState, WorkflowEvent], WorkflowState] = {
    (WorkflowState.START, WorkflowEvent.PING_OK): WorkflowState.PING_DB,
    (WorkflowState.PING_DB, WorkflowEvent.PING_OK): WorkflowState.PING_DB,
    (WorkflowState.PING_DB, WorkflowEvent.PING_FAIL): WorkflowState.CHECK_LOGS,
    (WorkflowState.CHECK_LOGS, WorkflowEvent.LOGS_COLLECTED): WorkflowState.OPEN_INCIDENT,
    (WorkflowState.OPEN_INCIDENT, WorkflowEvent.INCIDENT_OPENED): WorkflowState.CHECK_CAP,
    (WorkflowState.CHECK_CAP, WorkflowEvent.CAP_AVAILABLE): WorkflowState.DIAGNOSE,
    (WorkflowState.CHECK_CAP, WorkflowEvent.CAP_EXCEEDED): WorkflowState.CAP_REACHED,
    (WorkflowState.DIAGNOSE, WorkflowEvent.DIAGNOSED): WorkflowState.CHOOSE_ACTION,
    (WorkflowState.CHOOSE_ACTION, WorkflowEvent.ACTION_CHOSEN): WorkflowState.VALIDATE_ACTION,
    (WorkflowState.VALIDATE_ACTION, WorkflowEvent.ACTION_VALID): WorkflowState.EXECUTE,
    (WorkflowState.EXECUTE, WorkflowEvent.ACTION_EXECUTED): WorkflowState.VERIFY,
    (WorkflowState.VERIFY, WorkflowEvent.VERIFIED_HEALTHY): WorkflowState.UPDATE_AND_LEARN,
    (WorkflowState.VERIFY, WorkflowEvent.VERIFIED_UNHEALTHY): WorkflowState.CHECK_LOGS,
    (WorkflowState.UPDATE_AND_LEARN, WorkflowEvent.LEARNED): WorkflowState.RESOLVED,
}


def transition(state: WorkflowState, event: WorkflowEvent) -> WorkflowState:
    key = (state, event)
    if key not in _TRANSITIONS:
        raise ValueError(f"Invalid transition: {state} --{event}--> ?")
    return _TRANSITIONS[key]
