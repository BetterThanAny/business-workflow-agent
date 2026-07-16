from collections.abc import Callable
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from business_workflow_agent.auth import Principal, Role
from business_workflow_agent.models import AuditEvent, Ticket, TicketEvent, ToolCall


def test_ticket_and_event_are_rolled_back_if_event_persistence_fails(
    client: TestClient,
    session: Session,
    principal_factory: Callable[..., Principal],
    auth_headers: Callable[[Principal], dict[str, str]],
    create_run: Callable[[Principal], UUID],
    create_customer: Callable[[Principal, str], dict[str, object]],
) -> None:
    admin = principal_factory(Role.ADMIN)
    support = principal_factory(Role.SUPPORT_AGENT)
    customer = create_customer(admin, "rollback")
    run_id = create_run(support)

    def fail_ticket_event(*_args: object) -> None:
        raise RuntimeError("injected ticket event failure")

    event.listen(TicketEvent, "before_insert", fail_ticket_event)
    try:
        response = client.post(
            "/api/v1/tickets",
            json={
                "customer_id": customer["id"],
                "title": "Atomic creation",
                "description": "Ticket event insert will fail",
                "priority": "HIGH",
            },
            headers={
                **auth_headers(support),
                "X-Workflow-Run-ID": str(run_id),
                "Idempotency-Key": "ticket-rollback-injected",
            },
        )
    finally:
        event.remove(TicketEvent, "before_insert", fail_ticket_event)

    assert response.status_code == 500
    assert response.json()["detail"] == "INTERNAL_ERROR"
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Ticket)) == 0
    assert session.scalar(select(func.count()).select_from(TicketEvent)) == 0
    assert (
        session.scalar(
            select(func.count()).select_from(ToolCall).where(
                ToolCall.tool_name == "create_ticket",
                ToolCall.status == "FAILED",
            )
        )
        == 1
    )
    assert (
        session.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.event_type == "TOOL_CALL_FAILED"
            )
        )
        == 1
    )

