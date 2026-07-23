import json
from uuid import uuid4

import httpx
import pytest

from business_workflow_agent.tools.registry import build_tool_registry
from business_workflow_agent.workflow.provider import (
    AgentIntent,
    DeterministicProvider,
    OpenAICompatibleHttpTransport,
    OpenAICompatibleProvider,
    ProviderAuthenticationError,
    ProviderMalformedOutputError,
    ProviderRequest,
    ProviderRetryExhaustedError,
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


def _provider_response(content: object, *, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"total_tokens": 19},
        },
    )


def test_http_transport_sends_strict_schema_and_uses_reported_usage() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _provider_response(
            json.dumps(
                {
                    "intent": "SEARCH_KNOWLEDGE",
                    "tool_name": "search_knowledge_base",
                    "arguments": {"query": "MFA"},
                    "missing_fields": [],
                    "thought_summary": "Use knowledge.",
                    "usage": {"tokens": 999, "cost_cents": 999},
                }
            )
        )

    transport = OpenAICompatibleHttpTransport(
        base_url="http://provider.test/v1",
        api_key="secret-marker",
        timeout_seconds=1,
        max_attempts=1,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _seconds: None,
    )
    provider = OpenAICompatibleProvider(transport, model="live-model")

    proposal = provider.classify(ProviderRequest(message="search knowledge"))

    assert proposal.usage.tokens == 19
    assert proposal.usage.cost_cents == 0
    request = requests[0]
    assert request.url == "http://provider.test/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer secret-marker"
    payload = json.loads(request.content)
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert payload["response_format"]["json_schema"]["schema"]["title"] == "IntentProposal"


@pytest.mark.parametrize("status_code", [401, 403])
def test_http_transport_rejects_authentication_without_leaking_secret(
    status_code: int,
) -> None:
    transport = OpenAICompatibleHttpTransport(
        base_url="http://provider.test/v1",
        api_key="do-not-leak-this",
        timeout_seconds=1,
        max_attempts=1,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(status_code, json={"error": "denied"})
            )
        ),
    )

    with pytest.raises(ProviderAuthenticationError) as caught:
        transport.complete_json(model="m", messages=[], json_schema={})
    assert "do-not-leak-this" not in str(caught.value)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not-json"),
        _provider_response("not-json"),
        _provider_response("[]"),
        httpx.Response(200, json={"choices": []}),
    ],
)
def test_http_transport_rejects_malformed_success(response: httpx.Response) -> None:
    transport = OpenAICompatibleHttpTransport(
        base_url="http://provider.test/v1",
        api_key=None,
        timeout_seconds=1,
        max_attempts=1,
        client=httpx.Client(transport=httpx.MockTransport(lambda _request: response)),
    )
    with pytest.raises(ProviderMalformedOutputError):
        transport.complete_json(model="m", messages=[], json_schema={})


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_http_transport_bounds_retryable_failures(status_code: int) -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status_code)

    transport = OpenAICompatibleHttpTransport(
        base_url="http://provider.test/v1",
        api_key=None,
        timeout_seconds=1,
        max_attempts=2,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _seconds: None,
    )
    with pytest.raises(ProviderRetryExhaustedError):
        transport.complete_json(model="m", messages=[], json_schema={})
    assert attempts == 2
