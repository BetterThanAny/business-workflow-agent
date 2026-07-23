import json
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Any, Protocol, cast

import httpx
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


class ProviderAuthenticationError(ProviderError):
    code = "PROVIDER_AUTHENTICATION"


class ProviderClientError(ProviderError):
    code = "PROVIDER_CLIENT_ERROR"


class ProviderRetryExhaustedError(ProviderError):
    code = "PROVIDER_RETRY_EXHAUSTED"


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


class OpenAICompatibleHttpTransport:
    """Strict, bounded HTTP transport for OpenAI-compatible chat completions."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        timeout_seconds: float,
        max_attempts: int,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("provider max_attempts must be at least one")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.client = client or httpx.Client()
        self._owns_client = client is None
        self.sleeper = sleeper

    def complete_json(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any],
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_response",
                    "strict": True,
                    "schema": json_schema,
                },
            },
        }
        last_retryable: ProviderError | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.client.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=self.timeout_seconds,
                )
            except httpx.TimeoutException:
                last_retryable = ProviderTimeoutError("provider request timed out")
            except httpx.NetworkError:
                last_retryable = ProviderServerError(503)
            else:
                if response.status_code in {401, 403}:
                    raise ProviderAuthenticationError("provider authentication failed")
                if response.status_code == 429:
                    last_retryable = ProviderRateLimitError("provider rate limited the request")
                elif 500 <= response.status_code <= 599:
                    last_retryable = ProviderServerError(response.status_code)
                elif not 200 <= response.status_code < 300:
                    raise ProviderClientError(
                        f"provider returned non-retryable HTTP {response.status_code}"
                    )
                else:
                    return self._parse_response(response)
            if attempt < self.max_attempts:
                self.sleeper(min(0.1 * (2 ** (attempt - 1)), 0.5))
        raise ProviderRetryExhaustedError(
            last_retryable.code if last_retryable is not None else "provider request failed"
        )

    @staticmethod
    def _parse_response(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            parsed = json.loads(content) if isinstance(content, str) else content
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderMalformedOutputError("provider returned malformed JSON") from exc
        if not isinstance(parsed, dict):
            raise ProviderMalformedOutputError("provider JSON response must be an object")
        result = cast(dict[str, Any], parsed)
        if "usage" in result:
            response_usage = body.get("usage", {})
            result["usage"] = {
                "tokens": int(response_usage.get("total_tokens", 0)),
                "cost_cents": 0,
            }
        return result

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


class OpenAICompatibleProvider:
    """Thin structured-output adapter for an injected OpenAI-compatible transport."""

    def __init__(
        self,
        transport: StructuredCompletionTransport,
        *,
        model: str,
        tool_catalog: list[dict[str, Any]] | None = None,
    ) -> None:
        self.transport = transport
        self.model = model
        self.tool_catalog = tool_catalog or []

    def classify(self, request: ProviderRequest) -> IntentProposal:
        schema = IntentProposal.model_json_schema()
        registered_names = [
            str(tool["name"]) for tool in self.tool_catalog if isinstance(tool.get("name"), str)
        ]
        if registered_names:
            schema["properties"]["tool_name"] = {
                "anyOf": [
                    {"type": "string", "enum": registered_names},
                    {"type": "null"},
                ]
            }
        compact_catalog = [
            {
                "name": tool["name"],
                "required": tool["input_schema"].get("required", []),
                "properties": tool["input_schema"].get("properties", {}),
            }
            for tool in self.tool_catalog
            if isinstance(tool.get("name"), str) and isinstance(tool.get("input_schema"), dict)
        ]
        payload = self._complete(
            (
                "Classify intent, select exactly one registered tool, and extract arguments. "
                "Use values from both message and context. Never invent a tool name. "
                "Set missing_fields only for required input fields that are absent. "
                f"Registered tools: {json.dumps(compact_catalog, sort_keys=True)}"
            ),
            request.model_dump_json(),
            schema,
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

    def close(self) -> None:
        close = getattr(self.transport, "close", None)
        if callable(close):
            close()


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
