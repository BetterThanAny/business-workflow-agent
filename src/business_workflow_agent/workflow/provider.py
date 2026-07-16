from enum import StrEnum
from typing import Any, Protocol, cast

from pydantic import Field, StrictInt

from business_workflow_agent.schemas import StrictModel


class ProviderError(RuntimeError):
    code = "PROVIDER_ERROR"
    retryable = False


class ProviderRateLimitError(ProviderError):
    code = "PROVIDER_RATE_LIMIT"
    retryable = True


class ProviderTimeoutError(ProviderError):
    code = "PROVIDER_TIMEOUT"
    retryable = True


class ProviderServerError(ProviderError):
    retryable = True

    def __init__(self, status_code: int) -> None:
        if status_code < 500 or status_code > 599:
            raise ValueError("provider server status must be between 500 and 599")
        self.status_code = status_code
        self.code = f"PROVIDER_SERVER_{status_code}"
        super().__init__(self.code)


class ProviderMalformedOutputError(ProviderError):
    code = "PROVIDER_MALFORMED_OUTPUT"


class AgentIntent(StrEnum):
    SEARCH_KNOWLEDGE = "SEARCH_KNOWLEDGE"
    GET_CUSTOMER = "GET_CUSTOMER"
    LIST_CUSTOMER_TICKETS = "LIST_CUSTOMER_TICKETS"
    CREATE_TICKET = "CREATE_TICKET"
    UPDATE_TICKET = "UPDATE_TICKET"
    CALCULATE_REFUND = "CALCULATE_REFUND"
    ISSUE_REFUND = "ISSUE_REFUND"
    REQUEST_HUMAN_APPROVAL = "REQUEST_HUMAN_APPROVAL"
    UNKNOWN = "UNKNOWN"


INTENT_TOOL_NAMES: dict[AgentIntent, str] = {
    AgentIntent.SEARCH_KNOWLEDGE: "search_knowledge_base",
    AgentIntent.GET_CUSTOMER: "get_customer",
    AgentIntent.LIST_CUSTOMER_TICKETS: "list_customer_tickets",
    AgentIntent.CREATE_TICKET: "create_ticket",
    AgentIntent.UPDATE_TICKET: "update_ticket",
    AgentIntent.CALCULATE_REFUND: "calculate_refund",
    AgentIntent.ISSUE_REFUND: "issue_refund",
    AgentIntent.REQUEST_HUMAN_APPROVAL: "request_human_approval",
}


class ModelUsage(StrictModel):
    tokens: StrictInt = Field(ge=0)
    cost_cents: StrictInt = Field(ge=0)


class ProviderRequest(StrictModel):
    message: str
    context: dict[str, Any] = Field(default_factory=dict)


class IntentProposal(StrictModel):
    intent: AgentIntent
    tool_name: str | None
    arguments: dict[str, Any]
    missing_fields: list[str]
    thought_summary: str
    usage: ModelUsage


class RepairRequest(StrictModel):
    message: str
    context: dict[str, Any]
    proposal: IntentProposal
    validation_errors: list[dict[str, Any]]
    input_schema: dict[str, Any]


class SummaryRequest(StrictModel):
    message: str
    tool_name: str
    result: dict[str, Any]


class SummaryResponse(StrictModel):
    summary: str
    usage: ModelUsage


class StructuredProvider(Protocol):
    def classify(self, request: ProviderRequest) -> IntentProposal: ...

    def repair(self, request: RepairRequest) -> IntentProposal: ...

    def summarize(self, request: SummaryRequest) -> SummaryResponse: ...


class StructuredCompletionTransport(Protocol):
    def complete_json(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any],
    ) -> dict[str, Any]: ...


class OpenAICompatibleProvider:
    """Thin structured-output adapter for an injected OpenAI-compatible transport."""

    def __init__(self, transport: StructuredCompletionTransport, *, model: str) -> None:
        self.transport = transport
        self.model = model

    def classify(self, request: ProviderRequest) -> IntentProposal:
        payload = self._complete(
            "Classify intent, select one registered tool, and extract arguments.",
            request.model_dump_json(),
            IntentProposal.model_json_schema(),
        )
        return IntentProposal.model_validate(payload)

    def repair(self, request: RepairRequest) -> IntentProposal:
        payload = self._complete(
            "Repair the proposed arguments from the supplied validation errors exactly once.",
            request.model_dump_json(),
            IntentProposal.model_json_schema(),
        )
        return IntentProposal.model_validate(payload)

    def summarize(self, request: SummaryRequest) -> SummaryResponse:
        payload = self._complete(
            "Summarize the verified tool result without inventing fields.",
            request.model_dump_json(),
            SummaryResponse.model_json_schema(),
        )
        return SummaryResponse.model_validate(payload)

    def _complete(
        self,
        system_message: str,
        user_message: str,
        json_schema: dict[str, Any],
    ) -> dict[str, Any]:
        return self.transport.complete_json(
            model=self.model,
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": user_message},
            ],
            json_schema=json_schema,
        )


class DeterministicProvider:
    """Deterministic default test stub; it performs no remote or paid calls."""

    def classify(self, request: ProviderRequest) -> IntentProposal:
        intent = self._classify_intent(request.message)
        tool_name = INTENT_TOOL_NAMES.get(intent)
        arguments = self._arguments(intent, request.message, request.context)
        missing_fields = self._missing_fields(intent, arguments)
        return IntentProposal(
            intent=intent,
            tool_name=tool_name,
            arguments=arguments,
            missing_fields=missing_fields,
            thought_summary=f"Classified the request as {intent.value}.",
            usage=ModelUsage(tokens=20, cost_cents=1),
        )

    def repair(self, request: RepairRequest) -> IntentProposal:
        replacement = request.context.get("repair_arguments")
        arguments = (
            dict(cast(dict[str, Any], replacement))
            if isinstance(replacement, dict)
            else dict(request.proposal.arguments)
        )
        return request.proposal.model_copy(
            update={
                "arguments": arguments,
                "missing_fields": self._missing_fields(request.proposal.intent, arguments),
                "thought_summary": "Repaired arguments using the validation feedback.",
                "usage": ModelUsage(tokens=10, cost_cents=0),
            }
        )

    def summarize(self, request: SummaryRequest) -> SummaryResponse:
        return SummaryResponse(
            summary=f"{request.tool_name} completed with a verified result.",
            usage=ModelUsage(tokens=10, cost_cents=0),
        )

    def _classify_intent(self, message: str) -> AgentIntent:
        normalized = message.lower()
        patterns: tuple[tuple[AgentIntent, tuple[str, ...]], ...] = (
            (AgentIntent.REQUEST_HUMAN_APPROVAL, ("request human approval", "请求人工审批")),
            (AgentIntent.ISSUE_REFUND, ("issue refund", "执行退款")),
            (AgentIntent.CALCULATE_REFUND, ("calculate refund", "计算退款")),
            (AgentIntent.UPDATE_TICKET, ("update ticket", "更新工单")),
            (AgentIntent.CREATE_TICKET, ("create ticket", "创建工单")),
            (AgentIntent.LIST_CUSTOMER_TICKETS, ("list customer tickets", "工单列表")),
            (AgentIntent.GET_CUSTOMER, ("get customer", "查询客户")),
            (AgentIntent.SEARCH_KNOWLEDGE, ("search knowledge", "搜索知识")),
        )
        for intent, phrases in patterns:
            if any(phrase in normalized for phrase in phrases):
                return intent
        return AgentIntent.UNKNOWN

    def _arguments(
        self,
        intent: AgentIntent,
        message: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        allowed_fields: dict[AgentIntent, tuple[str, ...]] = {
            AgentIntent.SEARCH_KNOWLEDGE: ("query", "limit"),
            AgentIntent.GET_CUSTOMER: ("customer_id",),
            AgentIntent.LIST_CUSTOMER_TICKETS: ("customer_id", "limit"),
            AgentIntent.CREATE_TICKET: (
                "customer_id",
                "title",
                "description",
                "priority",
            ),
            AgentIntent.UPDATE_TICKET: (
                "ticket_id",
                "expected_version",
                "title",
                "description",
                "priority",
                "status",
            ),
            AgentIntent.CALCULATE_REFUND: (
                "order_id",
                "purchase_amount_cents",
                "requested_amount_cents",
                "currency",
                "reason",
            ),
            AgentIntent.ISSUE_REFUND: (
                "quote_id",
                "order_id",
                "purchase_amount_cents",
                "amount_cents",
                "currency",
                "reason",
            ),
            AgentIntent.REQUEST_HUMAN_APPROVAL: (
                "tool_name",
                "tool_arguments",
                "reason",
                "expires_in_seconds",
            ),
        }
        arguments = {
            field: context[field]
            for field in allowed_fields.get(intent, ())
            if field in context
        }
        if intent is AgentIntent.SEARCH_KNOWLEDGE and "query" not in arguments:
            arguments["query"] = message
        return arguments

    def _missing_fields(
        self,
        intent: AgentIntent,
        arguments: dict[str, Any],
    ) -> list[str]:
        required: dict[AgentIntent, tuple[str, ...]] = {
            AgentIntent.SEARCH_KNOWLEDGE: ("query",),
            AgentIntent.GET_CUSTOMER: ("customer_id",),
            AgentIntent.LIST_CUSTOMER_TICKETS: ("customer_id",),
            AgentIntent.CREATE_TICKET: ("customer_id", "title", "description"),
            AgentIntent.UPDATE_TICKET: ("ticket_id", "expected_version"),
            AgentIntent.CALCULATE_REFUND: (
                "order_id",
                "purchase_amount_cents",
                "requested_amount_cents",
                "reason",
            ),
            AgentIntent.ISSUE_REFUND: (
                "quote_id",
                "order_id",
                "purchase_amount_cents",
                "amount_cents",
                "reason",
            ),
            AgentIntent.REQUEST_HUMAN_APPROVAL: (
                "tool_name",
                "tool_arguments",
                "reason",
            ),
            AgentIntent.UNKNOWN: ("intent",),
        }
        missing = [field for field in required.get(intent, ()) if field not in arguments]
        if intent is AgentIntent.UPDATE_TICKET and not any(
            field in arguments for field in ("title", "description", "priority", "status")
        ):
            missing.append("change")
        return missing
