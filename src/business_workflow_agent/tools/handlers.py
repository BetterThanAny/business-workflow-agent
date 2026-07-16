from uuid import UUID

from business_workflow_agent.auth import Principal
from business_workflow_agent.knowledge import (
    DeterministicKnowledgeBackend,
    KnowledgeBackend,
)
from business_workflow_agent.schemas import (
    ApprovalOutput,
    CalculateRefundInput,
    CreateTicketInput,
    CustomerOutput,
    GetCustomerInput,
    IssueRefundInput,
    ListCustomerTicketsInput,
    RefundOutput,
    RefundQuoteOutput,
    RequestHumanApprovalInput,
    SearchKnowledgeBaseInput,
    SearchKnowledgeBaseOutput,
    TicketListOutput,
    TicketOutput,
    UpdateTicketInput,
)
from business_workflow_agent.services import BusinessService


def build_search_knowledge_handler(backend: KnowledgeBackend):
    def search_knowledge_base(
        _service: BusinessService,
        data: SearchKnowledgeBaseInput,
        principal: Principal,
        _run_id: UUID,
    ) -> SearchKnowledgeBaseOutput:
        return backend.search(tenant_id=principal.tenant_id, data=data)

    return search_knowledge_base


search_knowledge_base = build_search_knowledge_handler(DeterministicKnowledgeBackend())


def get_customer(
    service: BusinessService,
    data: GetCustomerInput,
    principal: Principal,
    _run_id: UUID,
) -> CustomerOutput:
    return CustomerOutput.model_validate(service.get_customer(principal, data.customer_id))


def list_customer_tickets(
    service: BusinessService,
    data: ListCustomerTicketsInput,
    principal: Principal,
    _run_id: UUID,
) -> TicketListOutput:
    tickets = service.list_customer_tickets(principal, data.customer_id, limit=data.limit)
    return TicketListOutput(tickets=[TicketOutput.model_validate(ticket) for ticket in tickets])


def create_ticket(
    service: BusinessService,
    data: CreateTicketInput,
    principal: Principal,
    _run_id: UUID,
) -> TicketOutput:
    return TicketOutput.model_validate(service.create_ticket(principal, data))


def update_ticket(
    service: BusinessService,
    data: UpdateTicketInput,
    principal: Principal,
    _run_id: UUID,
) -> TicketOutput:
    return TicketOutput.model_validate(service.update_ticket(principal, data))


def calculate_refund(
    service: BusinessService,
    data: CalculateRefundInput,
    principal: Principal,
    _run_id: UUID,
) -> RefundQuoteOutput:
    quote_id, eligible, approved, policy_reason = service.calculate_refund(principal, data)
    return RefundQuoteOutput(
        quote_id=quote_id,
        order_id=data.order_id,
        eligible=eligible,
        approved_amount_cents=approved,
        currency=data.currency,
        policy_reason=policy_reason,
    )


def issue_refund(
    service: BusinessService,
    data: IssueRefundInput,
    principal: Principal,
    _run_id: UUID,
) -> RefundOutput:
    return RefundOutput.model_validate(service.issue_refund(principal, data))


def request_human_approval(
    service: BusinessService,
    data: RequestHumanApprovalInput,
    principal: Principal,
    run_id: UUID,
) -> ApprovalOutput:
    approval = service.request_approval(
        principal,
        run_id,
        data,
        redacted_arguments={"redacted": True},
    )
    return ApprovalOutput.model_validate(approval)
