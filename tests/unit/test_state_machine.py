from __future__ import annotations

import pytest

from mi_agent.workflow.state_machine import WorkflowEvent, WorkflowState, transition


def test_workflow_happy_path_transitions() -> None:
    state = WorkflowState.START
    state = transition(state, WorkflowEvent.PING_OK)
    assert state is WorkflowState.PING_DB

    state = transition(state, WorkflowEvent.PING_FAIL)
    assert state is WorkflowState.CHECK_LOGS

    state = transition(state, WorkflowEvent.LOGS_COLLECTED)
    state = transition(state, WorkflowEvent.INCIDENT_OPENED)
    state = transition(state, WorkflowEvent.CAP_AVAILABLE)
    state = transition(state, WorkflowEvent.DIAGNOSED)
    state = transition(state, WorkflowEvent.ACTION_CHOSEN)
    state = transition(state, WorkflowEvent.ACTION_VALID)
    state = transition(state, WorkflowEvent.ACTION_EXECUTED)
    state = transition(state, WorkflowEvent.VERIFIED_HEALTHY)
    state = transition(state, WorkflowEvent.LEARNED)

    assert state is WorkflowState.RESOLVED


def test_invalid_transition_rejected() -> None:
    with pytest.raises(ValueError):
        transition(WorkflowState.START, WorkflowEvent.ACTION_EXECUTED)
