from datetime import datetime
from typing import Annotated, Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, model_validator

from business_workflow_agent.domain import (
    ApprovalDecision,
    ApprovalStatus,
    RefundStatus,
    TicketPriority,
    TicketStatus,
    ToolCallStatus,
    ToolExecutionStatus,
    WorkflowState,
)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
EmailAddress = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=3,
        max_length=320,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    ),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class SearchKnowledgeBaseInput(StrictModel):
    query: NonEmptyStr
    limit: StrictInt = Field(default=5, ge=1, le=20)


class KnowledgeArticle(StrictModel):
    article_id: str
    title: str
    excerpt: str


class SearchKnowledgeBaseOutput(StrictModel):
    articles: list[KnowledgeArticle]


class GetCustomerInput(StrictModel):
    customer_id: UUID


class CustomerCreateInput(StrictModel):
    external_id: NonEmptyStr = Field(max_length=100)
    name: NonEmptyStr = Field(max_length=200)
    email: EmailAddress


class CustomerOutput(StrictModel):
    id: UUID
    tenant_id: UUID
    external_id: str
    name: str
    email: str


class CustomerCreatedOutput(StrictModel):
    id: UUID
    tenant_id: UUID
    external_id: str


class ListCustomerTicketsInput(StrictModel):
    customer_id: UUID
    limit: StrictInt = Field(default=50, ge=1, le=100)


class CreateTicketInput(StrictModel):
    customer_id: UUID
    title: NonEmptyStr = Field(max_length=200)
    description: NonEmptyStr = Field(max_length=5000)
    priority: TicketPriority = TicketPriority.NORMAL


class UpdateTicketInput(StrictModel):
    ticket_id: UUID
    expected_version: StrictInt = Field(ge=1)
    title: NonEmptyStr | None = Field(default=None, max_length=200)
    description: NonEmptyStr | None = Field(default=None, max_length=5000)
    priority: TicketPriority | None = None
    status: TicketStatus | None = None

    @model_validator(mode="after")
    def require_change(self) -> Self:
        if all(
            value is None
            for value in (self.title, self.description, self.priority, self.status)
        ):
            raise ValueError("at least one ticket field must change")
        return self


class TicketOutput(StrictModel):
    id: UUID
    tenant_id: UUID
    customer_id: UUID
    title: str
    priority: TicketPriority
    status: TicketStatus
    version: int


class TicketListOutput(StrictModel):
    tickets: list[TicketOutput]


class CalculateRefundInput(StrictModel):
    order_id: NonEmptyStr = Field(max_length=100)
    purchase_amount_cents: StrictInt = Field(gt=0, le=100_000_000)
    requested_amount_cents: StrictInt = Field(gt=0, le=100_000_000)
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] = "CNY"
    reason: NonEmptyStr = Field(max_length=1000)


class RefundQuoteOutput(StrictModel):
    quote_id: UUID
    order_id: str
    eligible: bool
    approved_amount_cents: int
    currency: str
    policy_reason: str


class IssueRefundInput(StrictModel):
    quote_id: UUID
    order_id: NonEmptyStr = Field(max_length=100)
    purchase_amount_cents: StrictInt = Field(gt=0, le=100_000_000)
    amount_cents: StrictInt = Field(gt=0, le=100_000_000)
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] = "CNY"
    reason: NonEmptyStr = Field(max_length=1000)


class RefundOutput(StrictModel):
    id: UUID
    tenant_id: UUID
    order_id: str
    amount_cents: int
    currency: str
    status: RefundStatus


class RequestHumanApprovalInput(StrictModel):
    tool_name: NonEmptyStr = Field(max_length=100)
    tool_arguments: dict[str, Any]
    reason: NonEmptyStr = Field(max_length=1000)
    expires_in_seconds: StrictInt = Field(default=3600, ge=60, le=86_400)


class ApprovalOutput(StrictModel):
    id: UUID
    tenant_id: UUID
    run_id: UUID
    tool_name: str
    status: ApprovalStatus


class ApprovalDetailOutput(ApprovalOutput):
    requested_by_user_id: UUID
    tool_arguments_redacted: dict[str, Any]
    expires_at: datetime
    decided_by_user_id: UUID | None = None


class ApprovalTokenOutput(StrictModel):
    approval_id: UUID
    decision_token: str = Field(min_length=32)
    expires_at: datetime


class ApprovalDecisionInput(StrictModel):
    decision: ApprovalDecision
    decision_token: str = Field(min_length=16, max_length=500)


class ApprovalDecisionOutput(StrictModel):
    approval_id: UUID
    run_id: UUID
    approval_status: ApprovalStatus
    tool_call_status: ToolCallStatus
    run_state: WorkflowState
    result: dict[str, Any] | None = None


class WorkflowBudget(StrictModel):
    max_steps: StrictInt = Field(default=20, ge=1, le=100)
    max_tool_calls: StrictInt = Field(default=10, ge=1, le=50)
    max_elapsed_seconds: StrictInt = Field(default=300, ge=1, le=3600)
    max_tokens: StrictInt = Field(default=20_000, ge=1)
    max_cost_cents: StrictInt = Field(default=100, ge=0)


class WorkflowRunCreateInput(StrictModel):
    budget: WorkflowBudget = Field(default_factory=WorkflowBudget)


class WorkflowRunOutput(StrictModel):
    id: UUID
    tenant_id: UUID
    user_id: UUID
    state: WorkflowState
    version: int
    budget: dict[str, Any]


class ToolExecutionRequest(StrictModel):
    run_id: UUID
    arguments: dict[str, Any]
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=200)


class ToolExecutionResponse(StrictModel):
    status: ToolExecutionStatus
    tool_call_id: UUID
    result: dict[str, Any] | None = None
    error: str | None = None
    approval_id: UUID | None = None
    replayed: bool = False


class ToolSchemaListOutput(StrictModel):
    tools: list[dict[str, Any]]


class AgentRunCreateInput(StrictModel):
    message: NonEmptyStr = Field(max_length=5000)
    context: dict[str, Any] = Field(default_factory=dict)
    budget: WorkflowBudget = Field(default_factory=WorkflowBudget)


class AgentRunResumeInput(StrictModel):
    message: NonEmptyStr | None = Field(default=None, max_length=5000)
    context: dict[str, Any] = Field(default_factory=dict)


class AgentManualResumeInput(StrictModel):
    reason: NonEmptyStr = Field(max_length=1000)
    message: NonEmptyStr | None = Field(default=None, max_length=5000)
    context: dict[str, Any] = Field(default_factory=dict)
    budget: WorkflowBudget | None = None


class AgentRunOutput(StrictModel):
    id: UUID
    tenant_id: UUID
    user_id: UUID
    state: WorkflowState
    version: int
    budget: dict[str, Any]
    step_count: int
    tool_call_count: int
    tokens_used: int
    cost_cents_used: int
    schema_repair_attempts: int
    pending_fields: list[str]
    result: dict[str, Any] | None = None
    summary: str | None = None
    error_code: str | None = None
    retry_count: int
    retry_from_state: WorkflowState | None = None
    next_retry_at: datetime | None = None
    cancel_requested_at: datetime | None = None


class AgentEventOutput(StrictModel):
    sequence: int
    event_type: str
    payload: dict[str, Any]


class RunTrajectoryItem(StrictModel):
    kind: str
    occurred_at: datetime
    state: str | None = None
    status: str | None = None
    tool_name: str | None = None
    error_code: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class RunTrajectoryOutput(StrictModel):
    run_id: UUID
    state: WorkflowState
    version: int
    error_code: str | None = None
    items: list[RunTrajectoryItem]
