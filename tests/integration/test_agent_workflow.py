from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from business_workflow_agent.auth import Principal, Role
from business_workflow_agent.models import (
    Approval,
    Ticket,
    ToolCall,
    WorkflowCheckpoint,
    WorkflowEvent,
    WorkflowRun,
)


def test_agent_clarifies_missing_fields_then_completes_and_streams_events(
    client: TestClient,
    session: Session,
    principal_factory: Callable[..., Principal],
    auth_headers: Callable[[Principal], dict[str, str]],
    create_customer: Callable[[Principal, str], dict[str, object]],
) -> None:
    admin = principal_factory(Role.ADMIN)
    support = principal_factory(Role.SUPPORT_AGENT)
    customer = create_customer(admin, "agent-clarify")
    start = client.post(
        "/api/v1/agent-runs",
        json={
            "message": "create ticket",
            "context": {"customer_id": customer["id"], "title": "Cannot sign in"},
        },
        headers=auth_headers(support),
    )
    assert start.status_code == 201, start.text
    assert start.json()["state"] == "CLARIFY"
    assert set(start.json()["pending_fields"]) == {"description"}
    run_id = start.json()["id"]

    resumed = client.post(
        f"/api/v1/agent-runs/{run_id}/resume",
        json={"context": {"description": "MFA challenge loops", "priority": "HIGH"}},
        headers=auth_headers(support),
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["state"] == "COMPLETE"
    assert resumed.json()["result"]["status"] == "OPEN"

    stream = client.get(
        f"/api/v1/agent-runs/{run_id}/events",
        headers=auth_headers(support),
    )
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert "event: thought_summary" in stream.text
    assert "event: tool_proposed" in stream.text
    assert "event: complete" in stream.text

    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Ticket)) == 1
    assert session.scalar(select(func.count()).select_from(ToolCall)) >= 1
    checkpoints = list(
        session.scalars(
            select(WorkflowCheckpoint)
            .where(WorkflowCheckpoint.run_id == UUID(run_id))
            .order_by(WorkflowCheckpoint.version)
        )
    )
    assert len(checkpoints) == resumed.json()["version"]
    assert [checkpoint.state for checkpoint in checkpoints] == [
        "RECEIVED",
        "CLASSIFY",
        "CLARIFY",
        "CLASSIFY",
        "PLAN_ACTION",
        "VALIDATE_POLICY",
        "EXECUTE",
        "VERIFY_RESULT",
        "COMPLETE",
    ]
    assert (
        session.scalar(
            select(func.count()).select_from(WorkflowEvent).where(
                WorkflowEvent.run_id == UUID(run_id)
            )
        )
        >= 3
    )


def test_schema_validation_is_repaired_at_most_once(
    engine: object,
    principal_factory: Callable[..., Principal],
) -> None:
    from business_workflow_agent.db import create_session_factory
    from business_workflow_agent.schemas import AgentRunCreateInput
    from business_workflow_agent.tools.registry import build_tool_registry
    from business_workflow_agent.workflow.provider import (
        AgentIntent,
        IntentProposal,
        ModelUsage,
        SummaryResponse,
    )
    from business_workflow_agent.workflow.runner import AgentRunner

    class RepairingProvider:
        def __init__(self) -> None:
            self.repair_calls = 0

        def classify(self, _request: object) -> IntentProposal:
            return IntentProposal(
                intent=AgentIntent.SEARCH_KNOWLEDGE,
                tool_name="search_knowledge_base",
                arguments={"query": "MFA", "limit": 0},
                missing_fields=[],
                thought_summary="Search the knowledge base.",
                usage=ModelUsage(tokens=10, cost_cents=1),
            )

        def repair(self, _request: object) -> IntentProposal:
            self.repair_calls += 1
            return IntentProposal(
                intent=AgentIntent.SEARCH_KNOWLEDGE,
                tool_name="search_knowledge_base",
                arguments={"query": "MFA", "limit": 5},
                missing_fields=[],
                thought_summary="Repair the invalid limit.",
                usage=ModelUsage(tokens=5, cost_cents=0),
            )

        def summarize(self, _request: object) -> SummaryResponse:
            return SummaryResponse(
                summary="Knowledge search completed.",
                usage=ModelUsage(tokens=5, cost_cents=0),
            )

    provider = RepairingProvider()
    runner = AgentRunner(create_session_factory(engine), build_tool_registry(), provider)
    support = principal_factory(Role.SUPPORT_AGENT)
    run = runner.create(support, AgentRunCreateInput(message="search knowledge"))

    completed = runner.run_to_pause(support, run.id)

    assert completed.state == "COMPLETE"
    assert completed.schema_repair_attempts == 1
    assert provider.repair_calls == 1
    factory = create_session_factory(engine)
    with factory() as database_session:
        checkpoint_states = list(
            database_session.scalars(
                select(WorkflowCheckpoint.state)
                .where(WorkflowCheckpoint.run_id == run.id)
                .order_by(WorkflowCheckpoint.version)
            )
        )
    assert checkpoint_states == [
        "RECEIVED",
        "CLASSIFY",
        "RETRIEVE",
        "VALIDATE_POLICY",
        "REPAIR_SCHEMA",
        "VALIDATE_POLICY",
        "EXECUTE",
        "VERIFY_RESULT",
        "COMPLETE",
    ]


def test_second_invalid_schema_result_pauses_without_a_second_repair(
    engine: object,
    principal_factory: Callable[..., Principal],
) -> None:
    from business_workflow_agent.db import create_session_factory
    from business_workflow_agent.schemas import AgentRunCreateInput
    from business_workflow_agent.tools.registry import build_tool_registry
    from business_workflow_agent.workflow.provider import (
        AgentIntent,
        IntentProposal,
        ModelUsage,
        SummaryResponse,
    )
    from business_workflow_agent.workflow.runner import AgentRunner

    class NonRepairingProvider:
        def __init__(self) -> None:
            self.repair_calls = 0

        def _invalid(self) -> IntentProposal:
            return IntentProposal(
                intent=AgentIntent.SEARCH_KNOWLEDGE,
                tool_name="search_knowledge_base",
                arguments={"query": "MFA", "limit": 0},
                missing_fields=[],
                thought_summary="Invalid limit.",
                usage=ModelUsage(tokens=1, cost_cents=0),
            )

        def classify(self, _request: object) -> IntentProposal:
            return self._invalid()

        def repair(self, _request: object) -> IntentProposal:
            self.repair_calls += 1
            return self._invalid()

        def summarize(self, _request: object) -> SummaryResponse:
            raise AssertionError("an invalid proposal must never execute")

    provider = NonRepairingProvider()
    runner = AgentRunner(create_session_factory(engine), build_tool_registry(), provider)
    support = principal_factory(Role.SUPPORT_AGENT)
    run = runner.create(support, AgentRunCreateInput(message="search knowledge"))

    paused = runner.run_to_pause(support, run.id)
    replayed = runner.run_to_pause(support, run.id)

    assert paused.state == replayed.state == "CLARIFY"
    assert paused.error_code == "SCHEMA_VALIDATION_FAILED"
    assert paused.schema_repair_attempts == 1
    assert paused.version == replayed.version
    assert provider.repair_calls == 1


def test_step_and_token_budgets_terminate_stably(
    engine: object,
    principal_factory: Callable[..., Principal],
) -> None:
    from business_workflow_agent.db import create_session_factory
    from business_workflow_agent.schemas import AgentRunCreateInput, WorkflowBudget
    from business_workflow_agent.tools.registry import build_tool_registry
    from business_workflow_agent.workflow.provider import DeterministicProvider
    from business_workflow_agent.workflow.runner import AgentRunner

    runner = AgentRunner(
        create_session_factory(engine), build_tool_registry(), DeterministicProvider()
    )
    support = principal_factory(Role.SUPPORT_AGENT)
    step_limited = runner.create(
        support,
        AgentRunCreateInput(
            message="search knowledge",
            context={"query": "MFA"},
            budget=WorkflowBudget(max_steps=2),
        ),
    )
    first = runner.run_to_pause(support, step_limited.id)
    second = runner.run_to_pause(support, step_limited.id)
    assert first.state == second.state == "MANUAL_REVIEW"
    assert first.error_code == second.error_code == "MAX_STEPS_EXCEEDED"
    assert first.version == second.version

    token_limited = runner.create(
        support,
        AgentRunCreateInput(
            message="search knowledge",
            context={"query": "MFA"},
            budget=WorkflowBudget(max_tokens=1),
        ),
    )
    exhausted = runner.run_to_pause(support, token_limited.id)
    exhausted_replay = runner.run_to_pause(support, token_limited.id)
    assert exhausted.state == "MANUAL_REVIEW"
    assert exhausted.error_code == "TOKEN_BUDGET_EXCEEDED"
    assert exhausted_replay.version == exhausted.version

    cost_limited = runner.create(
        support,
        AgentRunCreateInput(
            message="search knowledge",
            context={"query": "MFA"},
            budget=WorkflowBudget(max_cost_cents=0),
        ),
    )
    cost_exhausted = runner.run_to_pause(support, cost_limited.id)
    cost_replay = runner.run_to_pause(support, cost_limited.id)
    assert cost_exhausted.state == "MANUAL_REVIEW"
    assert cost_exhausted.error_code == "COST_BUDGET_EXCEEDED"
    assert cost_replay.version == cost_exhausted.version

    future = datetime.now(UTC) + timedelta(days=1)
    elapsed_runner = AgentRunner(
        create_session_factory(engine),
        build_tool_registry(),
        DeterministicProvider(),
        clock=lambda: future,
    )
    elapsed_limited = elapsed_runner.create(
        support,
        AgentRunCreateInput(
            message="search knowledge",
            context={"query": "MFA"},
            budget=WorkflowBudget(max_elapsed_seconds=1),
        ),
    )
    elapsed = elapsed_runner.run_to_pause(support, elapsed_limited.id)
    elapsed_replay = elapsed_runner.run_to_pause(support, elapsed_limited.id)
    assert elapsed.state == "MANUAL_REVIEW"
    assert elapsed.error_code == "MAX_ELAPSED_TIME_EXCEEDED"
    assert elapsed_replay.version == elapsed.version

    tool_limited = runner.create(
        support,
        AgentRunCreateInput(
            message="search knowledge",
            context={"query": "MFA"},
            budget=WorkflowBudget(max_tool_calls=1),
        ),
    )
    while runner.get(support, tool_limited.id).state != "EXECUTE":
        runner.advance_once(support, tool_limited.id)
    factory = create_session_factory(engine)
    with factory.begin() as database_session:
        persisted = database_session.get(WorkflowRun, tool_limited.id)
        assert persisted is not None
        persisted.tool_call_count = 1
    tool_exhausted = runner.run_to_pause(support, tool_limited.id)
    tool_replay = runner.run_to_pause(support, tool_limited.id)
    assert tool_exhausted.state == "MANUAL_REVIEW"
    assert tool_exhausted.error_code == "MAX_TOOL_CALLS_EXCEEDED"
    assert tool_replay.version == tool_exhausted.version


def test_high_risk_agent_pauses_and_streams_approval_required(
    client: TestClient,
    session: Session,
    principal_factory: Callable[..., Principal],
    auth_headers: Callable[[Principal], dict[str, str]],
    create_run: Callable[[Principal], UUID],
) -> None:
    manager = principal_factory(Role.REFUND_MANAGER)
    quote_run = create_run(manager)
    quote_request = {
        "order_id": "agent-order-1",
        "purchase_amount_cents": 5000,
        "requested_amount_cents": 1000,
        "currency": "CNY",
        "reason": "Duplicate shipment",
    }
    quote_response = client.post(
        "/api/v1/refunds/quote",
        json=quote_request,
        headers={**auth_headers(manager), "X-Workflow-Run-ID": str(quote_run)},
    )
    assert quote_response.status_code == 200
    quote = quote_response.json()

    started = client.post(
        "/api/v1/agent-runs",
        json={
            "message": "issue refund",
            "context": {
                "quote_id": quote["quote_id"],
                "order_id": quote_request["order_id"],
                "purchase_amount_cents": quote_request["purchase_amount_cents"],
                "amount_cents": quote_request["requested_amount_cents"],
                "currency": quote_request["currency"],
                "reason": quote_request["reason"],
            },
        },
        headers=auth_headers(manager),
    )
    assert started.status_code == 201, started.text
    assert started.json()["state"] == "AWAIT_APPROVAL"
    agent_run_id = started.json()["id"]
    stream = client.get(
        f"/api/v1/agent-runs/{agent_run_id}/events",
        headers=auth_headers(manager),
    )
    assert "event: approval_required" in stream.text
    assert "event: complete" not in stream.text
    session.expire_all()
    assert (
        session.scalar(
            select(func.count()).select_from(Approval).where(
                Approval.run_id == UUID(agent_run_id)
            )
        )
        == 1
    )
    checkpoint_states = list(
        session.scalars(
            select(WorkflowCheckpoint.state)
            .where(WorkflowCheckpoint.run_id == UUID(agent_run_id))
            .order_by(WorkflowCheckpoint.version)
        )
    )
    assert checkpoint_states == [
        "RECEIVED",
        "CLASSIFY",
        "PLAN_ACTION",
        "VALIDATE_POLICY",
        "EXECUTE",
        "AWAIT_APPROVAL",
    ]


def test_unknown_request_pauses_for_clarification_without_calling_a_tool(
    engine: object,
    principal_factory: Callable[..., Principal],
) -> None:
    from business_workflow_agent.db import create_session_factory
    from business_workflow_agent.schemas import AgentRunCreateInput
    from business_workflow_agent.tools.registry import build_tool_registry
    from business_workflow_agent.workflow.provider import DeterministicProvider
    from business_workflow_agent.workflow.runner import AgentRunner

    factory = create_session_factory(engine)
    runner = AgentRunner(factory, build_tool_registry(), DeterministicProvider())
    support = principal_factory(Role.SUPPORT_AGENT)

    paused = runner.run_to_pause(
        support,
        runner.create(support, AgentRunCreateInput(message="please help me")).id,
    )

    assert paused.state == "CLARIFY"
    assert paused.pending_fields == ["intent"]
    with factory() as database_session:
        assert database_session.scalar(select(func.count()).select_from(ToolCall)) == 0
