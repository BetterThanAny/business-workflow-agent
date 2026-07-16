from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from business_workflow_agent.auth import Principal, Role
from business_workflow_agent.models import Approval, AuditEvent, Refund, ToolCall, WorkflowRun


def _start_refund_agent(
    client: TestClient,
    requester: Principal,
    headers: dict[str, str],
    create_run: Callable[[Principal], UUID],
    *,
    suffix: str,
) -> tuple[dict[str, object], dict[str, object]]:
    quote_run = create_run(requester)
    quote_request = {
        "order_id": f"approval-order-{suffix}",
        "purchase_amount_cents": 5000,
        "requested_amount_cents": 1000,
        "currency": "CNY",
        "reason": f"Sensitive approval reason {suffix}",
    }
    quote_response = client.post(
        "/api/v1/refunds/quote",
        json=quote_request,
        headers={**headers, "X-Workflow-Run-ID": str(quote_run)},
    )
    assert quote_response.status_code == 200, quote_response.text
    quote = quote_response.json()
    refund_arguments: dict[str, object] = {
        "quote_id": quote["quote_id"],
        "order_id": quote_request["order_id"],
        "purchase_amount_cents": quote_request["purchase_amount_cents"],
        "amount_cents": quote_request["requested_amount_cents"],
        "currency": quote_request["currency"],
        "reason": quote_request["reason"],
    }
    started = client.post(
        "/api/v1/agent-runs",
        json={"message": "issue refund", "context": refund_arguments},
        headers=headers,
    )
    assert started.status_code == 201, started.text
    assert started.json()["state"] == "AWAIT_APPROVAL"
    return started.json(), refund_arguments


def test_independent_approver_uses_one_time_token_and_resumes_workflow(
    client: TestClient,
    session: Session,
    principal_factory: Callable[..., Principal],
    auth_headers: Callable[[Principal], dict[str, str]],
    create_run: Callable[[Principal], UUID],
) -> None:
    requester = principal_factory(Role.REFUND_MANAGER)
    approver = principal_factory(Role.REFUND_MANAGER)
    requester_headers = auth_headers(requester)
    started, arguments = _start_refund_agent(
        client, requester, requester_headers, create_run, suffix="approve"
    )
    run_id = UUID(str(started["id"]))
    approval_id = UUID(str(started["result"]["approval_id"]))  # type: ignore[index]

    requester_token = client.post(
        f"/api/v1/approvals/{approval_id}/decision-token",
        headers=requester_headers,
    )
    assert requester_token.status_code == 403

    token_response = client.post(
        f"/api/v1/approvals/{approval_id}/decision-token",
        headers=auth_headers(approver),
    )
    assert token_response.status_code == 201, token_response.text
    decision_token = token_response.json()["decision_token"]
    details = client.get(
        f"/api/v1/approvals/{approval_id}",
        headers=auth_headers(approver),
    )
    assert details.status_code == 200
    assert details.json()["tool_arguments_redacted"]["reason"] == "[REDACTED]"
    assert str(arguments["reason"]) not in details.text
    session.expire_all()
    pending = session.get(Approval, approval_id)
    assert pending is not None
    assert pending.decision_token_hash is not None
    assert pending.decision_token_hash != decision_token
    assert len(pending.decision_token_hash) == 64

    decision = client.post(
        f"/api/v1/approvals/{approval_id}/decision",
        json={"decision": "APPROVE", "decision_token": decision_token},
        headers=auth_headers(approver),
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["approval_status"] == "USED"
    assert decision.json()["tool_call_status"] == "SUCCEEDED"
    assert decision.json()["run_state"] == "COMPLETE"

    repeated = client.post(
        f"/api/v1/approvals/{approval_id}/decision",
        json={"decision": "APPROVE", "decision_token": decision_token},
        headers=auth_headers(approver),
    )
    assert repeated.status_code == 409

    session.expire_all()
    approval = session.get(Approval, approval_id)
    assert approval is not None
    assert approval.status == "USED"
    assert approval.tool_arguments_available is True
    assert approval.decided_by_user_id == approver.user_id
    assert approval.decision_token_hash is None
    assert approval.decision_token_used_at is not None
    assert session.scalar(select(func.count()).select_from(Refund)) == 1
    call = session.scalar(select(ToolCall).where(ToolCall.approval_id == approval_id))
    assert call is not None
    assert call.status == "SUCCEEDED"
    run = session.get(WorkflowRun, run_id)
    assert run is not None and run.state == "COMPLETE"

    audit_events = list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.run_id == run_id)
            .order_by(AuditEvent.created_at)
        )
    )
    event_types = {event.event_type for event in audit_events}
    assert {"APPROVAL_TOKEN_ISSUED", "APPROVAL_APPROVED"} <= event_types
    persisted_audit = str([event.payload_redacted for event in audit_events])
    assert str(arguments["reason"]) not in persisted_audit
    assert decision_token not in persisted_audit
    assert all(event.tenant_id == requester.tenant_id for event in audit_events)
    assert all(event.run_id == run_id for event in audit_events)


def test_rejection_is_terminal_and_original_high_risk_call_cannot_resume(
    client: TestClient,
    session: Session,
    principal_factory: Callable[..., Principal],
    auth_headers: Callable[[Principal], dict[str, str]],
    create_run: Callable[[Principal], UUID],
) -> None:
    requester = principal_factory(Role.REFUND_MANAGER)
    approver = principal_factory(Role.ADMIN)
    requester_headers = auth_headers(requester)
    started, arguments = _start_refund_agent(
        client, requester, requester_headers, create_run, suffix="reject"
    )
    run_id = UUID(str(started["id"]))
    approval_id = UUID(str(started["result"]["approval_id"]))  # type: ignore[index]
    token_response = client.post(
        f"/api/v1/approvals/{approval_id}/decision-token",
        headers=auth_headers(approver),
    )
    decision_token = token_response.json()["decision_token"]

    rejected = client.post(
        f"/api/v1/approvals/{approval_id}/decision",
        json={"decision": "REJECT", "decision_token": decision_token},
        headers=auth_headers(approver),
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["approval_status"] == "REJECTED"
    assert rejected.json()["tool_call_status"] == "DENIED"
    assert rejected.json()["run_state"] == "NON_RETRYABLE_FAILURE"

    resume = client.post(
        f"/api/v1/agent-runs/{run_id}/resume",
        json={"context": {}},
        headers=requester_headers,
    )
    assert resume.status_code == 409
    replay = client.post(
        "/api/v1/tools/issue_refund/execute",
        json={
            "run_id": str(run_id),
            "idempotency_key": f"agent:{run_id}:tool:1",
            "arguments": arguments,
        },
        headers=requester_headers,
    )
    assert replay.status_code == 403
    assert replay.json()["replayed"] is True
    assert replay.json()["error"] == "APPROVAL_REJECTED"

    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Refund)) == 0
    assert (
        session.scalar(
            select(func.count()).select_from(Approval).where(Approval.id == approval_id)
        )
        == 1
    )


def test_expired_approval_cannot_execute_the_side_effect(
    client: TestClient,
    session: Session,
    principal_factory: Callable[..., Principal],
    auth_headers: Callable[[Principal], dict[str, str]],
    create_run: Callable[[Principal], UUID],
) -> None:
    requester = principal_factory(Role.REFUND_MANAGER)
    approver = principal_factory(Role.ADMIN)
    started, _arguments = _start_refund_agent(
        client, requester, auth_headers(requester), create_run, suffix="expired"
    )
    run_id = UUID(str(started["id"]))
    approval_id = UUID(str(started["result"]["approval_id"]))  # type: ignore[index]
    token_response = client.post(
        f"/api/v1/approvals/{approval_id}/decision-token",
        headers=auth_headers(approver),
    )
    decision_token = token_response.json()["decision_token"]
    approval = session.get(Approval, approval_id)
    assert approval is not None
    approval.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()

    expired = client.post(
        f"/api/v1/approvals/{approval_id}/decision",
        json={"decision": "APPROVE", "decision_token": decision_token},
        headers=auth_headers(approver),
    )
    assert expired.status_code == 409

    session.expire_all()
    approval = session.get(Approval, approval_id)
    run = session.get(WorkflowRun, run_id)
    assert approval is not None and approval.status == "EXPIRED"
    assert run is not None and run.state == "NON_RETRYABLE_FAILURE"
    assert session.scalar(select(func.count()).select_from(Refund)) == 0


def test_legacy_approval_without_executable_payload_is_terminalized(
    client: TestClient,
    session: Session,
    principal_factory: Callable[..., Principal],
    auth_headers: Callable[[Principal], dict[str, str]],
    create_run: Callable[[Principal], UUID],
) -> None:
    requester = principal_factory(Role.REFUND_MANAGER)
    approver = principal_factory(Role.ADMIN)
    started, _arguments = _start_refund_agent(
        client, requester, auth_headers(requester), create_run, suffix="legacy"
    )
    run_id = UUID(str(started["id"]))
    approval_id = UUID(str(started["result"]["approval_id"]))  # type: ignore[index]
    approval = session.get(Approval, approval_id)
    assert approval is not None
    approval.tool_arguments_available = False
    session.commit()

    token_response = client.post(
        f"/api/v1/approvals/{approval_id}/decision-token",
        headers=auth_headers(approver),
    )

    assert token_response.status_code == 409
    session.expire_all()
    approval = session.get(Approval, approval_id)
    call = session.scalar(select(ToolCall).where(ToolCall.approval_id == approval_id))
    run = session.get(WorkflowRun, run_id)
    assert approval is not None and approval.status == "EXPIRED"
    assert call is not None and call.status == "DENIED"
    assert call.error_code == "LEGACY_APPROVAL_REPROPOSAL_REQUIRED"
    assert run is not None and run.state == "NON_RETRYABLE_FAILURE"
    assert run.error_code == "LEGACY_APPROVAL_REPROPOSAL_REQUIRED"
    assert session.scalar(select(func.count()).select_from(Refund)) == 0
