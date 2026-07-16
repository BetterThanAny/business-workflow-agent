from collections.abc import Callable
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from business_workflow_agent.auth import Principal, Role
from business_workflow_agent.models import (
    Approval,
    AuditEvent,
    Refund,
    Ticket,
    TicketEvent,
    ToolCall,
)


def test_replaying_write_tool_ten_times_creates_one_side_effect(
    client: TestClient,
    session: Session,
    principal_factory: Callable[..., Principal],
    auth_headers: Callable[[Principal], dict[str, str]],
    create_run: Callable[[Principal], UUID],
    create_customer: Callable[[Principal, str], dict[str, object]],
) -> None:
    admin = principal_factory(Role.ADMIN)
    support = principal_factory(Role.SUPPORT_AGENT)
    customer = create_customer(admin, "idempotency")
    run_id = create_run(support)
    body = {
        "run_id": str(run_id),
        "idempotency_key": "ticket-tool-ten-replays",
        "arguments": {
            "customer_id": customer["id"],
            "title": "Repeated delivery",
            "description": "The same message arrived ten times.",
            "priority": "NORMAL",
        },
    }

    responses = [
        client.post(
            "/api/v1/tools/create_ticket/execute",
            json=body,
            headers=auth_headers(support),
        )
        for _ in range(10)
    ]

    assert {response.status_code for response in responses} == {200}
    assert len({response.json()["tool_call_id"] for response in responses}) == 1
    assert responses[0].json()["replayed"] is False
    assert all(response.json()["replayed"] is True for response in responses[1:])
    assert len({response.json()["result"]["id"] for response in responses}) == 1
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Ticket)) == 1
    assert session.scalar(select(func.count()).select_from(TicketEvent)) == 1
    assert (
        session.scalar(
            select(func.count()).select_from(ToolCall).where(
                ToolCall.tool_name == "create_ticket"
            )
        )
        == 1
    )
    assert (
        session.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.tool_name == "create_ticket"
            )
        )
        == 1
    )


def test_reusing_idempotency_key_with_different_arguments_is_rejected(
    client: TestClient,
    principal_factory: Callable[..., Principal],
    auth_headers: Callable[[Principal], dict[str, str]],
    create_run: Callable[[Principal], UUID],
    create_customer: Callable[[Principal, str], dict[str, object]],
) -> None:
    admin = principal_factory(Role.ADMIN)
    support = principal_factory(Role.SUPPORT_AGENT)
    customer = create_customer(admin, "idem-conflict")
    run_id = create_run(support)
    first_body = {
        "run_id": str(run_id),
        "idempotency_key": "ticket-conflicting-request",
        "arguments": {
            "customer_id": customer["id"],
            "title": "Original",
            "description": "First request",
            "priority": "LOW",
        },
    }
    first = client.post(
        "/api/v1/tools/create_ticket/execute",
        json=first_body,
        headers=auth_headers(support),
    )
    assert first.status_code == 200
    first_body["arguments"]["title"] = "Changed"

    second = client.post(
        "/api/v1/tools/create_ticket/execute",
        json=first_body,
        headers=auth_headers(support),
    )

    assert second.status_code == 409


def test_high_risk_tool_replays_one_approval_and_zero_refunds(
    client: TestClient,
    session: Session,
    principal_factory: Callable[..., Principal],
    auth_headers: Callable[[Principal], dict[str, str]],
    create_run: Callable[[Principal], UUID],
) -> None:
    manager = principal_factory(Role.REFUND_MANAGER)
    run_id = create_run(manager)
    quote_request = {
        "order_id": "order-approval",
        "purchase_amount_cents": 5000,
        "requested_amount_cents": 1000,
        "currency": "CNY",
        "reason": "Service outage",
    }
    quote = client.post(
        "/api/v1/refunds/quote",
        json=quote_request,
        headers={**auth_headers(manager), "X-Workflow-Run-ID": str(run_id)},
    ).json()
    body = {
        "run_id": str(run_id),
        "idempotency_key": "refund-high-risk-ten-replays",
        "arguments": {
            "quote_id": quote["quote_id"],
            "order_id": quote_request["order_id"],
            "purchase_amount_cents": quote_request["purchase_amount_cents"],
            "amount_cents": quote_request["requested_amount_cents"],
            "currency": quote_request["currency"],
            "reason": quote_request["reason"],
        },
    }

    responses = [
        client.post(
            "/api/v1/tools/issue_refund/execute",
            json=body,
            headers=auth_headers(manager),
        )
        for _ in range(10)
    ]

    assert {response.status_code for response in responses} == {202}
    assert len({response.json()["approval_id"] for response in responses}) == 1
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Approval)) == 1
    assert session.scalar(select(func.count()).select_from(Refund)) == 0
    approval = session.scalar(select(Approval))
    assert approval is not None
    assert approval.tool_arguments_redacted["reason"] == "[REDACTED]"
