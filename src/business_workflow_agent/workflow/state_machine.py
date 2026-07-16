from business_workflow_agent.domain import WorkflowState


class InvalidTransition(ValueError):
    pass


_DECLARED_TRANSITIONS: dict[WorkflowState, frozenset[WorkflowState]] = {
    WorkflowState.RECEIVED: frozenset({WorkflowState.CLASSIFY}),
    WorkflowState.CLASSIFY: frozenset(
        {WorkflowState.CLARIFY, WorkflowState.RETRIEVE, WorkflowState.PLAN_ACTION}
    ),
    WorkflowState.CLARIFY: frozenset({WorkflowState.CLASSIFY, WorkflowState.CANCELLED}),
    WorkflowState.RETRIEVE: frozenset({WorkflowState.VALIDATE_POLICY}),
    WorkflowState.PLAN_ACTION: frozenset({WorkflowState.VALIDATE_POLICY}),
    WorkflowState.VALIDATE_POLICY: frozenset(
        {WorkflowState.REPAIR_SCHEMA, WorkflowState.EXECUTE, WorkflowState.CLARIFY}
    ),
    WorkflowState.REPAIR_SCHEMA: frozenset({WorkflowState.VALIDATE_POLICY}),
    WorkflowState.EXECUTE: frozenset(
        {
            WorkflowState.VERIFY_RESULT,
            WorkflowState.AWAIT_APPROVAL,
            WorkflowState.MANUAL_REVIEW,
        }
    ),
    WorkflowState.VERIFY_RESULT: frozenset({WorkflowState.COMPLETE}),
    WorkflowState.AWAIT_APPROVAL: frozenset(
        {
            WorkflowState.VERIFY_RESULT,
            WorkflowState.NON_RETRYABLE_FAILURE,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.COMPLETE: frozenset(),
    WorkflowState.RETRYABLE_FAILURE: frozenset(
        {WorkflowState.RETRY, WorkflowState.CANCELLED}
    ),
    WorkflowState.RETRY: frozenset(
        {
            WorkflowState.CLASSIFY,
            WorkflowState.REPAIR_SCHEMA,
            WorkflowState.VERIFY_RESULT,
        }
    ),
    WorkflowState.NON_RETRYABLE_FAILURE: frozenset({WorkflowState.MANUAL_REVIEW}),
    WorkflowState.MANUAL_REVIEW: frozenset(
        {WorkflowState.CLASSIFY, WorkflowState.CANCELLED}
    ),
    WorkflowState.CANCELLED: frozenset(),
}

_FAILURE_TARGETS = frozenset(
    {
        WorkflowState.RETRYABLE_FAILURE,
        WorkflowState.NON_RETRYABLE_FAILURE,
        WorkflowState.MANUAL_REVIEW,
        WorkflowState.CANCELLED,
    }
)

PAUSE_STATES = frozenset(
    {
        WorkflowState.CLARIFY,
        WorkflowState.AWAIT_APPROVAL,
        WorkflowState.COMPLETE,
        WorkflowState.RETRYABLE_FAILURE,
        WorkflowState.NON_RETRYABLE_FAILURE,
        WorkflowState.MANUAL_REVIEW,
        WorkflowState.CANCELLED,
    }
)


def validate_transition(source: WorkflowState, target: WorkflowState) -> None:
    allowed = _DECLARED_TRANSITIONS[source]
    if source not in PAUSE_STATES:
        allowed = allowed | _FAILURE_TARGETS
    if target not in allowed:
        raise InvalidTransition(f"invalid workflow transition: {source.value} -> {target.value}")
