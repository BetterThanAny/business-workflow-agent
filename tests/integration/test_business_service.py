from collections.abc import Callable
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from business_workflow_agent.auth import Principal, Role
from business_workflow_agent.schemas import (
    CalculateRefundInput,
    CustomerCreateInput,
    IssueRefundInput,
    RequestHumanApprovalInput,
)
from business_workflow_agent.services import (
    BusinessService,
    InvalidRefund,
    RunOwnershipError,
)


def test_business_service_issues_only_an_exact_eligible_quote(
    session: Session,
    principal_factory: Callable[..., Principal],
) -> None:
    principal: Principal = principal_factory(Role.REFUND_MANAGER)
    service = BusinessService(session)
    quote_input = CalculateRefundInput(
        order_id="service-order",
        purchase_amount_cents=5000,
        requested_amount_cents=1250,
        currency="CNY",
        reason="Service regression",
    )
    quote_id, eligible, approved, reason = service.calculate_refund(
        principal,
        quote_input,
    )

    assert eligible is True
    assert approved == 1250
    assert reason == "within purchase amount"
    refund = service.issue_refund(
        principal,
        IssueRefundInput(
            quote_id=quote_id,
            order_id=quote_input.order_id,
            purchase_amount_cents=quote_input.purchase_amount_cents,
            amount_cents=quote_input.requested_amount_cents,
            currency=quote_input.currency,
            reason=quote_input.reason,
        ),
    )

    assert refund.tenant_id == principal.tenant_id
    assert refund.amount_cents == 1250
    assert refund.issued_by_user_id == principal.user_id


def test_business_service_rejects_an_ineligible_refund(
    session: Session,
    principal_factory: Callable[..., Principal],
) -> None:
    principal: Principal = principal_factory(Role.REFUND_MANAGER)
    service = BusinessService(session)
    ineligible = CalculateRefundInput(
        order_id="ineligible-order",
        purchase_amount_cents=1000,
        requested_amount_cents=1250,
        currency="CNY",
        reason="Too large",
    )
    quote_id, eligible, approved, reason = service.calculate_refund(
        principal,
        ineligible,
    )

    assert eligible is False
    assert approved == 0
    assert reason == "requested amount exceeds purchase amount"
    with pytest.raises(InvalidRefund, match="eligible quote") as raised:
        service.issue_refund(
            principal,
            IssueRefundInput(
                quote_id=quote_id,
                order_id=ineligible.order_id,
                purchase_amount_cents=ineligible.purchase_amount_cents,
                amount_cents=ineligible.requested_amount_cents,
                currency=ineligible.currency,
                reason=ineligible.reason,
            ),
        )
    assert raised.value.code == "INVALID_REFUND"


def test_business_service_persists_manual_approval(
    session: Session,
    principal_factory: Callable[..., Principal],
    create_run: Callable[[Principal], UUID],
) -> None:
    principal = principal_factory(Role.REFUND_MANAGER)
    run_id = create_run(principal)

    approval = BusinessService(session).request_approval(
        principal,
        run_id,
        RequestHumanApprovalInput(
            tool_name="issue_refund",
            tool_arguments={"reason": "sensitive"},
            reason="Manual review",
            expires_in_seconds=300,
        ),
        redacted_arguments={"reason": "[REDACTED]"},
    )

    assert approval.run_id == run_id
    assert approval.requested_by_user_id == principal.user_id
    assert approval.tool_arguments_redacted == {"reason": "[REDACTED]"}


def test_business_service_lists_empty_customer_tickets_and_enforces_run_owner(
    session: Session,
    principal_factory: Callable[..., Principal],
    create_run: Callable[[Principal], UUID],
) -> None:
    principal = principal_factory(Role.SUPPORT_AGENT)
    service = BusinessService(session)
    customer = service.create_customer(
        principal,
        CustomerCreateInput(
            external_id="empty-ticket-customer",
            name="Empty Ticket Customer",
            email="empty@example.com",
        ),
    )

    assert service.list_customer_tickets(principal, customer.id, limit=10) == []
    run_id = create_run(principal)
    other_user = principal_factory(Role.SUPPORT_AGENT, user_id=uuid4())
    with pytest.raises(RunOwnershipError, match="different user"):
        service.require_run(other_user, run_id)
