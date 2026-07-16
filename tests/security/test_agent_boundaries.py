from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from business_workflow_agent.auth import Principal, Role
from business_workflow_agent.db import create_session_factory
from business_workflow_agent.models import ToolCall
from business_workflow_agent.schemas import AgentRunCreateInput
from business_workflow_agent.tools.registry import build_tool_registry
from business_workflow_agent.workflow.provider import (
    AgentIntent,
    IntentProposal,
    ModelUsage,
    SummaryResponse,
)
from business_workflow_agent.workflow.runner import AgentRunner


class UnregisteredToolProvider:
    def classify(self, _request: object) -> IntentProposal:
        return IntentProposal(
            intent=AgentIntent.CREATE_TICKET,
            tool_name="dangerous_shell",
            arguments={},
            missing_fields=[],
            thought_summary="Attempt an unregistered tool.",
            usage=ModelUsage(tokens=5, cost_cents=0),
        )

    def repair(self, _request: object) -> IntentProposal:
        raise AssertionError("unregistered tools must be rejected before schema repair")

    def summarize(self, _request: object) -> SummaryResponse:
        raise AssertionError("unregistered tools must never execute")


class SensitiveTextProvider:
    def classify(self, _request: object) -> IntentProposal:
        return IntentProposal(
            intent=AgentIntent.SEARCH_KNOWLEDGE,
            tool_name="search_knowledge_base",
            arguments={"query": "customer@example.com password reset"},
            missing_fields=[],
            thought_summary="Search for customer@example.com.",
            usage=ModelUsage(tokens=5, cost_cents=0),
        )

    def repair(self, _request: object) -> IntentProposal:
        raise AssertionError("valid arguments do not need repair")

    def summarize(self, _request: object) -> SummaryResponse:
        return SummaryResponse(
            summary="Sent guidance to customer@example.com.",
            usage=ModelUsage(tokens=5, cost_cents=0),
        )


class MismatchedRegisteredToolProvider:
    def classify(self, _request: object) -> IntentProposal:
        return IntentProposal(
            intent=AgentIntent.CREATE_TICKET,
            tool_name="search_knowledge_base",
            arguments={"query": "ignore the requested action"},
            missing_fields=[],
            thought_summary="Try a registered tool under the wrong intent.",
            usage=ModelUsage(tokens=5, cost_cents=0),
        )

    def repair(self, _request: object) -> IntentProposal:
        raise AssertionError("a mismatched tool must be rejected before schema repair")

    def summarize(self, _request: object) -> SummaryResponse:
        raise AssertionError("a mismatched tool must never execute")

def test_agent_cannot_execute_provider_supplied_unregistered_tool(
    engine: object,
    session: Session,
    principal_factory: Callable[..., Principal],
) -> None:
    runner = AgentRunner(
        create_session_factory(engine), build_tool_registry(), UnregisteredToolProvider()
    )
    support = principal_factory(Role.SUPPORT_AGENT)
    run = runner.create(support, AgentRunCreateInput(message="ignore policy"))

    result = runner.run_to_pause(support, run.id)

    assert result.state == "MANUAL_REVIEW"
    assert result.error_code == "UNREGISTERED_TOOL"
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(ToolCall)) == 0


def test_agent_events_redact_provider_text_and_tool_arguments(
    engine: object,
    principal_factory: Callable[..., Principal],
) -> None:
    runner = AgentRunner(
        create_session_factory(engine), build_tool_registry(), SensitiveTextProvider()
    )
    support = principal_factory(Role.SUPPORT_AGENT)
    run = runner.create(support, AgentRunCreateInput(message="search knowledge"))

    completed = runner.run_to_pause(support, run.id)
    events = runner.events(support, run.id)
    persisted_text = str([event.model_dump(mode="json") for event in events])

    assert completed.state == "COMPLETE"
    assert "customer@example.com" not in persisted_text
    assert "[REDACTED]" in persisted_text


def test_agent_rejects_registered_tool_that_does_not_match_the_intent(
    engine: object,
    session: Session,
    principal_factory: Callable[..., Principal],
) -> None:
    runner = AgentRunner(
        create_session_factory(engine),
        build_tool_registry(),
        MismatchedRegisteredToolProvider(),
    )
    support = principal_factory(Role.SUPPORT_AGENT)
    run = runner.create(support, AgentRunCreateInput(message="create ticket"))

    result = runner.run_to_pause(support, run.id)

    assert result.state == "MANUAL_REVIEW"
    assert result.error_code == "TOOL_SELECTION_MISMATCH"
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(ToolCall)) == 0
