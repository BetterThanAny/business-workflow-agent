from collections.abc import Callable
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from business_workflow_agent.auth import Principal, Role
from business_workflow_agent.models import Refund, Ticket, TicketEvent


def test_ticket_update_and_refund_apis_commit_complete_transactions(
    client: TestClient,
    session: Session,
    principal_factory: Callable[..., Principal],
    auth_headers: Callable[[Principal], dict[str, str]],
    create_run: Callable[[Principal], UUID],
    create_customer: Callable[[Principal, str], dict[str, object]],
) -> None:
    admin = principal_factory(Role.ADMIN)
    approver = principal_factory(Role.REFUND_MANAGER)
    support = principal_factory(Role.SUPPORT_AGENT)
    customer = create_customer(admin, "happy")
    support_run = create_run(support)

    ticket_response = client.post(
        "/api/v1/tickets",
        json={
            "customer_id": customer["id"],
            "title": "Cannot sign in",
            "description": "MFA challenge loops after login.",
            "priority": "HIGH",
        },
        headers={
            **auth_headers(support),
            "X-Workflow-Run-ID": str(support_run),
            "Idempotency-Key": "ticket-create-happy",
        },
    )
    assert ticket_response.status_code == 201, ticket_response.text
    ticket = ticket_response.json()
    assert ticket["status"] == "OPEN"
    assert ticket["version"] == 1

    update_response = client.patch(
        f"/api/v1/tickets/{ticket['id']}",
        json={
            "ticket_id": ticket["id"],
            "expected_version": 1,
            "status": "PENDING",
        },
        headers={
            **auth_headers(support),
            "X-Workflow-Run-ID": str(support_run),
            "Idempotency-Key": "ticket-update-happy",
        },
    )
    assert update_response.status_code == 200, update_response.text
    assert update_response.json()["version"] == 2
    assert update_response.json()["status"] == "PENDING"

    admin_run = create_run(admin)
    quote_request = {
        "order_id": "order-100",
        "purchase_amount_cents": 12_000,
        "requested_amount_cents": 3_000,
        "currency": "CNY",
        "reason": "Duplicate shipment",
    }
    quote_response = client.post(
        "/api/v1/refunds/quote",
        json=quote_request,
        headers={
            **auth_headers(admin),
            "X-Workflow-Run-ID": str(admin_run),
        },
    )
    assert quote_response.status_code == 200, quote_response.text
    quote = quote_response.json()
    assert quote["eligible"] is True

    refund_response = client.post(
        "/api/v1/refunds",
        json={
            "quote_id": quote["quote_id"],
            "order_id": quote_request["order_id"],
            "purchase_amount_cents": quote_request["purchase_amount_cents"],
            "amount_cents": quote_request["requested_amount_cents"],
            "currency": quote_request["currency"],
            "reason": quote_request["reason"],
        },
        headers={
            **auth_headers(admin),
            "X-Workflow-Run-ID": str(admin_run),
            "Idempotency-Key": "refund-issue-happy",
        },
    )
    assert refund_response.status_code == 202, refund_response.text
    pending_refund = refund_response.json()
    assert pending_refund["status"] == "APPROVAL_REQUIRED"
    approval_id = pending_refund["approval_id"]

    for _ in range(9):
        replay = client.post(
            "/api/v1/refunds",
            json={
                "quote_id": quote["quote_id"],
                "order_id": quote_request["order_id"],
                "purchase_amount_cents": quote_request["purchase_amount_cents"],
                "amount_cents": quote_request["requested_amount_cents"],
                "currency": quote_request["currency"],
                "reason": quote_request["reason"],
            },
            headers={
                **auth_headers(admin),
                "X-Workflow-Run-ID": str(admin_run),
                "Idempotency-Key": "refund-issue-happy",
            },
        )
        assert replay.status_code == 202
        assert replay.json()["approval_id"] == approval_id
        assert replay.json()["replayed"] is True

    token = client.post(
        f"/api/v1/approvals/{approval_id}/decision-token",
        headers=auth_headers(approver),
    )
    assert token.status_code == 201, token.text
    decision = client.post(
        f"/api/v1/approvals/{approval_id}/decision",
        json={"decision": "APPROVE", "decision_token": token.json()["decision_token"]},
        headers=auth_headers(approver),
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["origin"] == "DIRECT_API"
    assert decision.json()["result"]["status"] == "ISSUED"
    assert decision.json()["run_state"] == "RECEIVED"

    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Ticket)) == 1
    assert session.scalar(select(func.count()).select_from(TicketEvent)) == 2
    assert session.scalar(select(func.count()).select_from(Refund)) == 1


def test_stale_ticket_update_and_invalid_refund_have_no_business_side_effect(
    client: TestClient,
    session: Session,
    principal_factory: Callable[..., Principal],
    auth_headers: Callable[[Principal], dict[str, str]],
    create_run: Callable[[Principal], UUID],
    create_customer: Callable[[Principal, str], dict[str, object]],
) -> None:
    admin = principal_factory(Role.ADMIN)
    support = principal_factory(Role.SUPPORT_AGENT)
    customer = create_customer(admin, "failure")
    support_run = create_run(support)
    created = client.post(
        "/api/v1/tickets",
        json={
            "customer_id": customer["id"],
            "title": "Password reset",
            "description": "Reset requested",
            "priority": "NORMAL",
        },
        headers={
            **auth_headers(support),
            "X-Workflow-Run-ID": str(support_run),
            "Idempotency-Key": "ticket-create-failure",
        },
    ).json()

    stale = client.patch(
        f"/api/v1/tickets/{created['id']}",
        json={"ticket_id": created["id"], "expected_version": 2, "status": "CLOSED"},
        headers={
            **auth_headers(support),
            "X-Workflow-Run-ID": str(support_run),
            "Idempotency-Key": "ticket-update-stale",
        },
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == "VERSION_CONFLICT"

    admin_run = create_run(admin)
    invalid_refund = client.post(
        "/api/v1/refunds",
        json={
            "quote_id": "f02fd5b9-67e1-4bc2-ac5b-d91dd7241800",
            "order_id": "order-invalid",
            "purchase_amount_cents": 1000,
            "amount_cents": 900,
            "currency": "CNY",
            "reason": "Invalid quote",
        },
        headers={
            **auth_headers(admin),
            "X-Workflow-Run-ID": str(admin_run),
            "Idempotency-Key": "refund-invalid-quote",
        },
    )
    assert invalid_refund.status_code == 202
    assert invalid_refund.json()["status"] == "APPROVAL_REQUIRED"

    session.expire_all()
    ticket = session.get(Ticket, UUID(created["id"]))
    assert ticket is not None
    assert ticket.status == "OPEN"
    assert ticket.version == 1
    assert session.scalar(select(func.count()).select_from(TicketEvent)) == 1
    assert session.scalar(select(func.count()).select_from(Refund)) == 0
