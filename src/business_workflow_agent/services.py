from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from business_workflow_agent.auth import Principal
from business_workflow_agent.domain import ApprovalStatus, RefundStatus, TicketStatus
from business_workflow_agent.models import (
    Approval,
    Customer,
    Refund,
    Ticket,
    TicketEvent,
    WorkflowRun,
)
from business_workflow_agent.schemas import (
    CalculateRefundInput,
    CreateTicketInput,
    CustomerCreateInput,
    IssueRefundInput,
    RequestHumanApprovalInput,
    UpdateTicketInput,
    WorkflowBudget,
)


class BusinessError(Exception):
    code = "BUSINESS_ERROR"


class ResourceNotFound(BusinessError):
    code = "NOT_FOUND"


class VersionConflict(BusinessError):
    code = "VERSION_CONFLICT"


class InvalidRefund(BusinessError):
    code = "INVALID_REFUND"


class RunOwnershipError(BusinessError):
    code = "RUN_OWNERSHIP_ERROR"


class BusinessService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_workflow_run(
        self,
        principal: Principal,
        budget: WorkflowBudget,
    ) -> WorkflowRun:
        run = WorkflowRun(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            budget=budget.model_dump(mode="json"),
        )
        self.session.add(run)
        self.session.flush()
        return run

    def require_run(self, principal: Principal, run_id: UUID) -> WorkflowRun:
        run = self.session.scalar(
            select(WorkflowRun).where(
                WorkflowRun.id == run_id,
                WorkflowRun.tenant_id == principal.tenant_id,
            )
        )
        if run is None:
            raise ResourceNotFound("workflow run not found")
        if run.user_id != principal.user_id:
            raise RunOwnershipError("workflow run belongs to a different user")
        return run

    def create_customer(
        self,
        principal: Principal,
        data: CustomerCreateInput,
    ) -> Customer:
        customer = Customer(
            tenant_id=principal.tenant_id,
            external_id=data.external_id,
            name=data.name,
            email=data.email,
        )
        self.session.add(customer)
        self.session.flush()
        return customer

    def get_customer(self, principal: Principal, customer_id: UUID) -> Customer:
        customer = self.session.scalar(
            select(Customer).where(
                Customer.id == customer_id,
                Customer.tenant_id == principal.tenant_id,
            )
        )
        if customer is None:
            raise ResourceNotFound("customer not found")
        return customer

    def list_customer_tickets(
        self,
        principal: Principal,
        customer_id: UUID,
        *,
        limit: int,
    ) -> list[Ticket]:
        self.get_customer(principal, customer_id)
        return list(
            self.session.scalars(
                select(Ticket)
                .where(
                    Ticket.customer_id == customer_id,
                    Ticket.tenant_id == principal.tenant_id,
                )
                .order_by(Ticket.created_at.desc())
                .limit(limit)
            )
        )

    def create_ticket(
        self,
        principal: Principal,
        data: CreateTicketInput,
    ) -> Ticket:
        self.get_customer(principal, data.customer_id)
        ticket = Ticket(
            tenant_id=principal.tenant_id,
            customer_id=data.customer_id,
            title=data.title,
            description=data.description,
            priority=data.priority.value,
            status=TicketStatus.OPEN.value,
        )
        self.session.add(ticket)
        self.session.flush()
        self.session.add(
            TicketEvent(
                tenant_id=principal.tenant_id,
                ticket_id=ticket.id,
                actor_user_id=principal.user_id,
                event_type="TICKET_CREATED",
                event_payload={"priority": data.priority.value},
            )
        )
        self.session.flush()
        return ticket

    def update_ticket(
        self,
        principal: Principal,
        data: UpdateTicketInput,
    ) -> Ticket:
        ticket = self.session.scalar(
            select(Ticket).where(
                Ticket.id == data.ticket_id,
                Ticket.tenant_id == principal.tenant_id,
            )
        )
        if ticket is None:
            raise ResourceNotFound("ticket not found")
        if ticket.version != data.expected_version:
            raise VersionConflict("ticket version does not match")

        changes: dict[str, object] = {}
        for field_name in ("title", "description", "priority", "status"):
            value = getattr(data, field_name)
            if value is None:
                continue
            stored_value = value.value if hasattr(value, "value") else value
            changes[field_name] = {"from": getattr(ticket, field_name), "to": stored_value}
            setattr(ticket, field_name, stored_value)
        ticket.version += 1
        self.session.add(
            TicketEvent(
                tenant_id=principal.tenant_id,
                ticket_id=ticket.id,
                actor_user_id=principal.user_id,
                event_type="TICKET_UPDATED",
                event_payload={"changes": changes, "version": ticket.version},
            )
        )
        self.session.flush()
        return ticket

    def calculate_refund(
        self,
        principal: Principal,
        data: CalculateRefundInput,
    ) -> tuple[UUID, bool, int, str]:
        eligible = data.requested_amount_cents <= data.purchase_amount_cents
        approved = data.requested_amount_cents if eligible else 0
        reason = (
            "within purchase amount"
            if eligible
            else "requested amount exceeds purchase amount"
        )
        quote_key = ":".join(
            [
                str(principal.tenant_id),
                data.order_id,
                str(data.purchase_amount_cents),
                str(data.requested_amount_cents),
                data.currency,
                data.reason,
            ]
        )
        return uuid5(NAMESPACE_URL, quote_key), eligible, approved, reason

    def issue_refund(self, principal: Principal, data: IssueRefundInput) -> Refund:
        quote_input = CalculateRefundInput(
            order_id=data.order_id,
            purchase_amount_cents=data.purchase_amount_cents,
            requested_amount_cents=data.amount_cents,
            currency=data.currency,
            reason=data.reason,
        )
        quote_id, eligible, approved, _ = self.calculate_refund(principal, quote_input)
        if not eligible or approved != data.amount_cents or quote_id != data.quote_id:
            raise InvalidRefund("refund does not match an eligible quote")
        refund = Refund(
            tenant_id=principal.tenant_id,
            order_id=data.order_id,
            amount_cents=data.amount_cents,
            currency=data.currency,
            reason=data.reason,
            status=RefundStatus.ISSUED.value,
            issued_by_user_id=principal.user_id,
        )
        self.session.add(refund)
        self.session.flush()
        return refund

    def request_approval(
        self,
        principal: Principal,
        run_id: UUID,
        data: RequestHumanApprovalInput,
        *,
        redacted_arguments: dict[str, object],
    ) -> Approval:
        self.require_run(principal, run_id)
        approval = Approval(
            tenant_id=principal.tenant_id,
            run_id=run_id,
            requested_by_user_id=principal.user_id,
            tool_name=data.tool_name,
            tool_arguments_redacted=redacted_arguments,
            status=ApprovalStatus.PENDING.value,
            expires_at=datetime.now(UTC) + timedelta(seconds=data.expires_in_seconds),
        )
        self.session.add(approval)
        self.session.flush()
        return approval
