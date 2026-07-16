from uuid import uuid4

from business_workflow_agent.tools.registry import build_tool_registry
from business_workflow_agent.workflow.provider import (
    AgentIntent,
    DeterministicProvider,
    OpenAICompatibleProvider,
    ProviderRequest,
)


def test_controlled_provider_scenarios_have_one_hundred_percent_valid_tool_arguments() -> None:
    provider = DeterministicProvider()
    registry = build_tool_registry()
    customer_id = str(uuid4())
    ticket_id = str(uuid4())
    quote_id = str(uuid4())
    scenarios = [
        ("search knowledge", {"query": "reset MFA"}),
        ("get customer", {"customer_id": customer_id}),
        ("list customer tickets", {"customer_id": customer_id, "limit": 10}),
        (
            "create ticket",
            {
                "customer_id": customer_id,
                "title": "Cannot sign in",
                "description": "MFA loop",
                "priority": "HIGH",
            },
        ),
        (
            "update ticket",
            {"ticket_id": ticket_id, "expected_version": 1, "status": "PENDING"},
        ),
        (
            "calculate refund",
            {
                "order_id": "order-1",
                "purchase_amount_cents": 5000,
                "requested_amount_cents": 1000,
                "currency": "CNY",
                "reason": "Duplicate shipment",
            },
        ),
        (
            "issue refund",
            {
                "quote_id": quote_id,
                "order_id": "order-1",
                "purchase_amount_cents": 5000,
                "amount_cents": 1000,
                "currency": "CNY",
                "reason": "Duplicate shipment",
            },
        ),
        (
            "request human approval",
            {
                "tool_name": "issue_refund",
                "tool_arguments": {"order_id": "order-1"},
                "reason": "Refund exceeds support limit",
                "expires_in_seconds": 3600,
            },
        ),
    ]

    proposals = [
        provider.classify(ProviderRequest(message=message, context=context))
        for message, context in scenarios
    ]

    assert all(not proposal.missing_fields for proposal in proposals)
    for proposal in proposals:
        assert proposal.tool_name is not None
        definition = registry.get(proposal.tool_name)
        definition.input_model.model_validate(proposal.arguments)


def test_provider_ignores_untrusted_tool_override_in_context() -> None:
    provider = DeterministicProvider()
    proposal = provider.classify(
        ProviderRequest(
            message="create ticket",
            context={
                "tool_name": "dangerous_shell",
                "customer_id": str(uuid4()),
                "title": "Safe",
                "description": "Use the registered ticket tool",
            },
        )
    )

    assert proposal.intent is AgentIntent.CREATE_TICKET
    assert proposal.tool_name == "create_ticket"


def test_openai_compatible_adapter_requests_and_validates_strict_json_schema() -> None:
    class RecordingTransport:
        def __init__(self) -> None:
            self.schema: dict[str, object] | None = None

        def complete_json(
            self,
            *,
            model: str,
            messages: list[dict[str, str]],
            json_schema: dict[str, object],
        ) -> dict[str, object]:
            assert model == "controlled-model"
            assert messages[0]["role"] == "system"
            self.schema = json_schema
            return {
                "intent": "SEARCH_KNOWLEDGE",
                "tool_name": "search_knowledge_base",
                "arguments": {"query": "MFA"},
                "missing_fields": [],
                "thought_summary": "Use the registered knowledge tool.",
                "usage": {"tokens": 7, "cost_cents": 1},
            }

    transport = RecordingTransport()
    provider = OpenAICompatibleProvider(transport, model="controlled-model")

    proposal = provider.classify(ProviderRequest(message="search knowledge"))

    assert proposal.intent is AgentIntent.SEARCH_KNOWLEDGE
    assert proposal.arguments == {"query": "MFA"}
    assert transport.schema is not None
    assert transport.schema["title"] == "IntentProposal"
