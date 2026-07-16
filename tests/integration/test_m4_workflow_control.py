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
    SideEffectOutbox,
    Ticket,
    ToolCall,
)
from business_workflow_agent.schemas import AgentRunCreateInput, WorkflowBudget


def test_cancelled_execute_state_never_starts_pending_write(
    client: TestClient,
    session: Session,
    principal_factory: Callable[..., Principal],
    auth_headers: Callable[[Principal], dict[str, str]],
    create_customer: Callable[[Principal, str], dict[str, object]],
) -> None:
    admin = principal_factory(Role.ADMIN)
    support = principal_factory(Role.SUPPORT_AGENT)
    customer = create_customer(admin, "cancel-before-write")
    runner = client.app.state.agent_runner  # type: ignore[attr-defined]
    run = runner.create(
        support,
        AgentRunCreateInput(
            message="create ticket",
            context={
                "customer_id": customer["id"],
                "title": "Must not be created",
                "description": "Cancellation wins before EXECUTE.",
            },
        ),
    )
    while runner.get(support, run.id).state != "EXECUTE":
        runner.advance_once(support, run.id)

    cancelled = client.post(
        f"/api/v1/agent-runs/{run.id}/cancel",
        headers=auth_headers(support),
    )

    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "CANCELLED"
    assert runner.run_to_pause(support, run.id).state == "CANCELLED"
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Ticket)) == 0
    assert session.scalar(select(func.count()).select_from(SideEffectOutbox)) == 0


def test_cancelled_approval_terminalizes_pending_high_risk_call(
    client: TestClient,
    session: Session,
    principal_factory: Callable[..., Principal],
    auth_headers: Callable[[Principal], dict[str, str]],
    create_run: Callable[[Principal], UUID],
) -> None:
    manager = principal_factory(Role.REFUND_MANAGER)
    quote_run = create_run(manager)
    quote_request = {
        "order_id": "cancelled-refund",
        "purchase_amount_cents": 5000,
        "requested_amount_cents": 1000,
        "currency": "CNY",
        "reason": "Cancel pending approval",
    }
    quote = client.post(
        "/api/v1/refunds/quote",
        json=quote_request,
        headers={**auth_headers(manager), "X-Workflow-Run-ID": str(quote_run)},
    ).json()
    started = client.post(
        "/api/v1/agent-runs",
        json={
            "message": "issue refund",
            "context": {
                "quote_id": quote["quote_id"],
                "order_id": quote_request["order_id"],
                "purchase_amount_cents": quote_request["purchase_amount_cents"],
                "amount_cents": quote_request["requested_amount_cents"],
                "currency": quote_request["currency"],
                "reason": quote_request["reason"],
            },
        },
        headers=auth_headers(manager),
    )
    run_id = UUID(started.json()["id"])

    cancelled = client.post(
        f"/api/v1/agent-runs/{run_id}/cancel", headers=auth_headers(manager)
    )

    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "CANCELLED"
    session.expire_all()
    approval = session.scalar(select(Approval).where(Approval.run_id == run_id))
    call = session.scalar(select(ToolCall).where(ToolCall.run_id == run_id))
    assert approval is not None and approval.status == "EXPIRED"
    assert call is not None and call.status == "DENIED"
    assert call.error_code == "WORKFLOW_CANCELLED"
    assert session.scalar(select(func.count()).select_from(Refund)) == 0


def test_manual_review_requires_explicit_reason_and_creates_new_proposal(
    client: TestClient,
    session: Session,
    principal_factory: Callable[..., Principal],
    auth_headers: Callable[[Principal], dict[str, str]],
) -> None:
    support = principal_factory(Role.SUPPORT_AGENT)
    started = client.post(
        "/api/v1/agent-runs",
        json={
            "message": "search knowledge",
            "context": {"query": "MFA"},
            "budget": WorkflowBudget(max_steps=1).model_dump(mode="json"),
        },
        headers=auth_headers(support),
    )
    run_id = UUID(started.json()["id"])
    assert started.json()["state"] == "MANUAL_REVIEW"

    missing_reason = client.post(
        f"/api/v1/agent-runs/{run_id}/manual-resume",
        json={},
        headers=auth_headers(support),
    )
    assert missing_reason.status_code == 422
    resumed = client.post(
        f"/api/v1/agent-runs/{run_id}/manual-resume",
        json={
            "reason": "Operator increased the run budget after reviewing the request.",
            "budget": WorkflowBudget(max_steps=20).model_dump(mode="json"),
        },
        headers=auth_headers(support),
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["state"] == "COMPLETE"
    session.expire_all()
    audits = list(
        session.scalars(
            select(AuditEvent).where(
                AuditEvent.run_id == run_id,
                AuditEvent.event_type == "MANUAL_REVIEW_RESUMED",
            )
        )
    )
    assert len(audits) == 1


def test_approved_refund_outbox_and_ten_decision_replays_create_one_refund(
    client: TestClient,
    session: Session,
    principal_factory: Callable[..., Principal],
    auth_headers: Callable[[Principal], dict[str, str]],
    create_run: Callable[[Principal], UUID],
) -> None:
    requester = principal_factory(Role.REFUND_MANAGER)
    approver = principal_factory(Role.ADMIN)
    quote_run = create_run(requester)
    quote_request = {
        "order_id": "outbox-refund-replay",
        "purchase_amount_cents": 5000,
        "requested_amount_cents": 1000,
        "currency": "CNY",
        "reason": "Outbox refund replay",
    }
    quote = client.post(
        "/api/v1/refunds/quote",
        json=quote_request,
        headers={**auth_headers(requester), "X-Workflow-Run-ID": str(quote_run)},
    ).json()
    started = client.post(
        "/api/v1/agent-runs",
        json={
            "message": "issue refund",
            "context": {
                "quote_id": quote["quote_id"],
                "order_id": quote_request["order_id"],
                "purchase_amount_cents": quote_request["purchase_amount_cents"],
                "amount_cents": quote_request["requested_amount_cents"],
                "currency": quote_request["currency"],
                "reason": quote_request["reason"],
            },
        },
        headers=auth_headers(requester),
    ).json()
    approval_id = UUID(started["result"]["approval_id"])
    token = client.post(
        f"/api/v1/approvals/{approval_id}/decision-token",
        headers=auth_headers(approver),
    ).json()["decision_token"]
    first = client.post(
        f"/api/v1/approvals/{approval_id}/decision",
        json={"decision": "APPROVE", "decision_token": token},
        headers=auth_headers(approver),
    )
    assert first.status_code == 200
    replays = [
        client.post(
            f"/api/v1/approvals/{approval_id}/decision",
            json={"decision": "APPROVE", "decision_token": token},
            headers=auth_headers(approver),
        )
        for _ in range(10)
    ]
    assert {response.status_code for response in replays} == {409}
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Refund)) == 1
    outbox = session.scalar(select(SideEffectOutbox))
    assert outbox is not None and outbox.status == "SUCCEEDED"
