import pytest

from business_workflow_agent.domain import WorkflowState
from business_workflow_agent.workflow.state_machine import InvalidTransition, validate_transition


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (WorkflowState.RECEIVED, WorkflowState.CLASSIFY),
        (WorkflowState.CLASSIFY, WorkflowState.CLARIFY),
        (WorkflowState.CLASSIFY, WorkflowState.RETRIEVE),
        (WorkflowState.CLASSIFY, WorkflowState.PLAN_ACTION),
        (WorkflowState.RETRIEVE, WorkflowState.VALIDATE_POLICY),
        (WorkflowState.PLAN_ACTION, WorkflowState.VALIDATE_POLICY),
        (WorkflowState.VALIDATE_POLICY, WorkflowState.REPAIR_SCHEMA),
        (WorkflowState.VALIDATE_POLICY, WorkflowState.EXECUTE),
        (WorkflowState.REPAIR_SCHEMA, WorkflowState.VALIDATE_POLICY),
        (WorkflowState.EXECUTE, WorkflowState.VERIFY_RESULT),
        (WorkflowState.EXECUTE, WorkflowState.AWAIT_APPROVAL),
        (WorkflowState.AWAIT_APPROVAL, WorkflowState.VERIFY_RESULT),
        (WorkflowState.AWAIT_APPROVAL, WorkflowState.NON_RETRYABLE_FAILURE),
        (WorkflowState.VERIFY_RESULT, WorkflowState.COMPLETE),
        (WorkflowState.CLARIFY, WorkflowState.CLASSIFY),
        (WorkflowState.RETRYABLE_FAILURE, WorkflowState.RETRY),
        (WorkflowState.RETRY, WorkflowState.CLASSIFY),
        (WorkflowState.MANUAL_REVIEW, WorkflowState.CLASSIFY),
        (WorkflowState.EXECUTE, WorkflowState.CANCELLED),
    ],
)
def test_explicit_state_machine_accepts_declared_transitions(
    source: WorkflowState,
    target: WorkflowState,
) -> None:
    validate_transition(source, target)


def test_explicit_state_machine_rejects_skipped_or_terminal_transitions() -> None:
    with pytest.raises(InvalidTransition):
        validate_transition(WorkflowState.RECEIVED, WorkflowState.EXECUTE)

    with pytest.raises(InvalidTransition):
        validate_transition(WorkflowState.COMPLETE, WorkflowState.CLASSIFY)
