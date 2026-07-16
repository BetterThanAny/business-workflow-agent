from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select

from business_workflow_agent.auth import Principal, Role
from business_workflow_agent.db import Base, create_database_engine, create_session_factory
from business_workflow_agent.domain import WorkflowState
from business_workflow_agent.models import WorkflowCheckpoint, WorkflowRun
from business_workflow_agent.schemas import AgentRunCreateInput, WorkflowBudget
from business_workflow_agent.tools.registry import build_tool_registry
from business_workflow_agent.workflow.persistence import build_checkpoint
from business_workflow_agent.workflow.provider import (
    AgentIntent,
    DeterministicProvider,
    IntentProposal,
    ModelUsage,
)
from business_workflow_agent.workflow.runner import AgentRunner
from business_workflow_agent.workflow.state_machine import PAUSE_STATES


def test_new_runner_resumes_from_every_committed_checkpoint(
    tmp_path: Path,
    principal_factory: Callable[..., Principal],
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'restart.db'}"
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    principal = principal_factory(Role.SUPPORT_AGENT)
    creator = AgentRunner(factory, build_tool_registry(), DeterministicProvider())
    run = creator.create(
        principal,
        AgentRunCreateInput(message="search knowledge", context={"query": "MFA"}),
    )
    seen_states = [run.state]

    while seen_states[-1] not in {"COMPLETE", "MANUAL_REVIEW"}:
        engine.dispose()
        engine = create_database_engine(database_url)
        factory = create_session_factory(engine)
        restarted = AgentRunner(factory, build_tool_registry(), DeterministicProvider())
        current = restarted.advance_once(principal, run.id)
        seen_states.append(current.state)

    assert seen_states == [
        "RECEIVED",
        "CLASSIFY",
        "RETRIEVE",
        "VALIDATE_POLICY",
        "EXECUTE",
        "VERIFY_RESULT",
        "COMPLETE",
    ]
    with factory() as session:
        checkpoints = list(
            session.scalars(
                select(WorkflowCheckpoint)
                .where(WorkflowCheckpoint.run_id == run.id)
                .order_by(WorkflowCheckpoint.version)
            )
        )
    assert [checkpoint.version for checkpoint in checkpoints] == list(
        range(1, len(checkpoints) + 1)
    )
    assert checkpoints[-1].state == "COMPLETE"
    engine.dispose()


def test_checkpoint_failure_rolls_back_the_state_transition(
    engine: object,
    principal_factory: Callable[..., Principal],
) -> None:
    factory = create_session_factory(engine)
    principal = principal_factory(Role.SUPPORT_AGENT)
    runner = AgentRunner(factory, build_tool_registry(), DeterministicProvider())
    run = runner.create(
        principal,
        AgentRunCreateInput(message="search knowledge", context={"query": "MFA"}),
    )

    def reject_checkpoint(*_args: object) -> None:
        raise RuntimeError("simulated checkpoint persistence failure")

    event.listen(WorkflowCheckpoint, "before_insert", reject_checkpoint)
    try:
        with pytest.raises(RuntimeError, match="checkpoint persistence failure"):
            runner.advance_once(principal, run.id)
    finally:
        event.remove(WorkflowCheckpoint, "before_insert", reject_checkpoint)

    with factory() as session:
        persisted = session.get(WorkflowRun, run.id)
        assert persisted is not None
        assert persisted.state == "RECEIVED"
        assert persisted.version == 1
        assert session.scalar(
            select(func.count()).select_from(WorkflowCheckpoint).where(
                WorkflowCheckpoint.run_id == run.id
            )
        ) == 1


def test_restarted_runner_handles_every_persisted_workflow_state(
    tmp_path: Path,
    principal_factory: Callable[..., Principal],
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'all-states.db'}"
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    principal = principal_factory(Role.SUPPORT_AGENT)
    usage = ModelUsage(tokens=1, cost_cents=0)
    search_proposal = IntentProposal(
        intent=AgentIntent.SEARCH_KNOWLEDGE,
        tool_name="search_knowledge_base",
        arguments={"query": "MFA"},
        missing_fields=[],
        thought_summary="restart coverage",
        usage=usage,
    ).model_dump(mode="json")
    create_proposal = IntentProposal(
        intent=AgentIntent.CREATE_TICKET,
        tool_name="create_ticket",
        arguments={
            "customer_id": str(uuid4()),
            "title": "Restart test",
            "description": "The missing customer produces an explicit failure.",
        },
        missing_fields=[],
        thought_summary="restart coverage",
        usage=usage,
    ).model_dump(mode="json")

    observed: set[WorkflowState] = set()
    for state in WorkflowState:
        proposal = None
        result_payload = None
        retry_from_state = None
        validation_errors: list[dict[str, object]] = []
        if state in {
            WorkflowState.RETRIEVE,
            WorkflowState.VALIDATE_POLICY,
            WorkflowState.EXECUTE,
            WorkflowState.VERIFY_RESULT,
        }:
            proposal = search_proposal
        elif state is WorkflowState.PLAN_ACTION:
            proposal = create_proposal
        elif state is WorkflowState.REPAIR_SCHEMA:
            proposal = {
                **search_proposal,
                "arguments": {"query": "MFA", "limit": "not-an-integer"},
            }
            validation_errors = [{"type": "int_type", "loc": ["limit"]}]
        elif state is WorkflowState.RETRY:
            retry_from_state = WorkflowState.CLASSIFY.value
        if state is WorkflowState.VERIFY_RESULT:
            result_payload = {"articles": []}

        with factory.begin() as session:
            run = WorkflowRun(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                state=state.value,
                version=1,
                budget=WorkflowBudget().model_dump(mode="json"),
                message="search knowledge",
                context_data={"query": "MFA"},
                proposal=proposal,
                result_payload=result_payload,
                validation_errors=validation_errors,
                retry_from_state=retry_from_state,
            )
            session.add(run)
            session.flush()
            session.add(build_checkpoint(run))
            run_id = run.id

        engine.dispose()
        engine = create_database_engine(database_url)
        factory = create_session_factory(engine)
        restarted = AgentRunner(factory, build_tool_registry(), DeterministicProvider())
        recovered = restarted.run_to_pause(principal, run_id)
        observed.add(state)
        if state in PAUSE_STATES:
            assert recovered.state is state
            assert recovered.version == 1
        else:
            assert recovered.state in PAUSE_STATES
            assert recovered.version > 1

    assert observed == set(WorkflowState)
    engine.dispose()
