import json
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from business_workflow_agent.app import create_app
from business_workflow_agent.auth import Principal, Role, create_access_token, granted_scopes
from business_workflow_agent.config import Settings
from business_workflow_agent.models import (
    Approval,
    Refund,
    SideEffectOutbox,
    Ticket,
    ToolCall,
    WorkflowCheckpoint,
    WorkflowEvent,
    WorkflowRun,
)
from business_workflow_agent.schemas import AgentRunCreateInput


def main() -> None:
    settings = Settings()  # type: ignore[call-arg]
    app = create_app(settings)
    tenant_id = uuid4()
    admin = _principal(tenant_id, Role.ADMIN)
    support = _principal(tenant_id, Role.SUPPORT_AGENT)
    manager = _principal(tenant_id, Role.REFUND_MANAGER)
    approver = _principal(tenant_id, Role.REFUND_MANAGER)

    with TestClient(app) as client:
        admin_headers = _auth_headers(admin, settings)
        support_headers = _auth_headers(support, settings)
        manager_headers = _auth_headers(manager, settings)
        approver_headers = _auth_headers(approver, settings)
        admin_run = _create_run(client, admin_headers)
        support_run = _create_run(client, support_headers)
        manager_run = _create_run(client, manager_headers)

        suffix = uuid4().hex
        customer_response = client.post(
            "/api/v1/customers",
            json={
                "external_id": f"smoke-{suffix}",
                "name": "Smoke Customer",
                "email": "customer@example.com",
            },
            headers={
                **admin_headers,
                "X-Workflow-Run-ID": str(admin_run),
                "Idempotency-Key": f"smoke-customer-{suffix}",
            },
        )
        _expect(customer_response, 201)
        customer_id = customer_response.json()["id"]

        create_ticket_request = {
            "run_id": str(support_run),
            "idempotency_key": f"smoke-ticket-{suffix}",
            "arguments": {
                "customer_id": customer_id,
                "title": "Smoke workflow ticket",
                "description": "Created by the deterministic M1 smoke test.",
                "priority": "HIGH",
            },
        }
        ticket_responses = [
            client.post(
                "/api/v1/tools/create_ticket/execute",
                json=create_ticket_request,
                headers=support_headers,
            )
            for _ in range(10)
        ]
        for response in ticket_responses:
            _expect(response, 200)
        ticket_ids = {response.json()["result"]["id"] for response in ticket_responses}
        assert len(ticket_ids) == 1
        assert all(response.json()["replayed"] for response in ticket_responses[1:])
        ticket_id = ticket_ids.pop()

        update_response = client.patch(
            f"/api/v1/tickets/{ticket_id}",
            json={
                "ticket_id": ticket_id,
                "expected_version": 1,
                "status": "PENDING",
            },
            headers={
                **support_headers,
                "X-Workflow-Run-ID": str(support_run),
                "Idempotency-Key": f"smoke-ticket-update-{suffix}",
            },
        )
        _expect(update_response, 200)

        quote_request = {
            "order_id": f"smoke-order-{suffix}",
            "purchase_amount_cents": 8000,
            "requested_amount_cents": 2000,
            "currency": "CNY",
            "reason": "Smoke test refund",
        }
        quote_response = client.post(
            "/api/v1/refunds/quote",
            json=quote_request,
            headers={**manager_headers, "X-Workflow-Run-ID": str(manager_run)},
        )
        _expect(quote_response, 200)
        quote = quote_response.json()
        refund_arguments = {
            "quote_id": quote["quote_id"],
            "order_id": quote_request["order_id"],
            "purchase_amount_cents": quote_request["purchase_amount_cents"],
            "amount_cents": quote_request["requested_amount_cents"],
            "currency": quote_request["currency"],
            "reason": quote_request["reason"],
        }
        approval_response = client.post(
            "/api/v1/tools/issue_refund/execute",
            json={
                "run_id": str(manager_run),
                "idempotency_key": f"smoke-refund-approval-{suffix}",
                "arguments": refund_arguments,
            },
            headers=manager_headers,
        )
        _expect(approval_response, 202)
        assert approval_response.json()["status"] == "APPROVAL_REQUIRED"

        knowledge_agent_response = client.post(
            "/api/v1/agent-runs",
            json={
                "message": "search knowledge",
                "context": {"query": "reset MFA"},
            },
            headers=support_headers,
        )
        _expect(knowledge_agent_response, 201)
        assert knowledge_agent_response.json()["state"] == "COMPLETE"
        knowledge_agent_id = UUID(knowledge_agent_response.json()["id"])
        knowledge_events = client.get(
            f"/api/v1/agent-runs/{knowledge_agent_id}/events",
            headers=support_headers,
        )
        _expect(knowledge_events, 200)
        assert "event: thought_summary" in knowledge_events.text
        assert "event: tool_proposed" in knowledge_events.text
        assert "event: complete" in knowledge_events.text

        refund_agent_response = client.post(
            "/api/v1/agent-runs",
            json={"message": "issue refund", "context": refund_arguments},
            headers=manager_headers,
        )
        _expect(refund_agent_response, 201)
        assert refund_agent_response.json()["state"] == "AWAIT_APPROVAL"
        refund_agent_id = UUID(refund_agent_response.json()["id"])
        refund_agent_events = client.get(
            f"/api/v1/agent-runs/{refund_agent_id}/events",
            headers=manager_headers,
        )
        _expect(refund_agent_events, 200)
        assert "event: approval_required" in refund_agent_events.text
        assert "event: complete" not in refund_agent_events.text

        refund_agent_approval_id = UUID(
            refund_agent_response.json()["result"]["approval_id"]
        )
        requester_token_response = client.post(
            f"/api/v1/approvals/{refund_agent_approval_id}/decision-token",
            headers=manager_headers,
        )
        _expect(requester_token_response, 403)
        rejection_token_response = client.post(
            f"/api/v1/approvals/{refund_agent_approval_id}/decision-token",
            headers=approver_headers,
        )
        _expect(rejection_token_response, 201)
        rejection_token = rejection_token_response.json()["decision_token"]
        rejection_response = client.post(
            f"/api/v1/approvals/{refund_agent_approval_id}/decision",
            json={"decision": "REJECT", "decision_token": rejection_token},
            headers=approver_headers,
        )
        _expect(rejection_response, 200)
        assert rejection_response.json()["approval_status"] == "REJECTED"
        assert rejection_response.json()["tool_call_status"] == "DENIED"
        assert rejection_response.json()["run_state"] == "NON_RETRYABLE_FAILURE"

        approved_quote_request = {
            **quote_request,
            "order_id": f"smoke-approved-order-{suffix}",
            "reason": "Approved smoke test refund",
        }
        approved_quote_response = client.post(
            "/api/v1/refunds/quote",
            json=approved_quote_request,
            headers={**manager_headers, "X-Workflow-Run-ID": str(manager_run)},
        )
        _expect(approved_quote_response, 200)
        approved_arguments = {
            "quote_id": approved_quote_response.json()["quote_id"],
            "order_id": approved_quote_request["order_id"],
            "purchase_amount_cents": approved_quote_request["purchase_amount_cents"],
            "amount_cents": approved_quote_request["requested_amount_cents"],
            "currency": approved_quote_request["currency"],
            "reason": approved_quote_request["reason"],
        }
        approved_agent_response = client.post(
            "/api/v1/agent-runs",
            json={"message": "issue refund", "context": approved_arguments},
            headers=manager_headers,
        )
        _expect(approved_agent_response, 201)
        assert approved_agent_response.json()["state"] == "AWAIT_APPROVAL"
        approved_agent_id = UUID(approved_agent_response.json()["id"])
        approved_approval_id = UUID(
            approved_agent_response.json()["result"]["approval_id"]
        )
        approval_token_response = client.post(
            f"/api/v1/approvals/{approved_approval_id}/decision-token",
            headers=approver_headers,
        )
        _expect(approval_token_response, 201)
        approval_token = approval_token_response.json()["decision_token"]
        approval_decision_response = client.post(
            f"/api/v1/approvals/{approved_approval_id}/decision",
            json={"decision": "APPROVE", "decision_token": approval_token},
            headers=approver_headers,
        )
        _expect(approval_decision_response, 200)
        assert approval_decision_response.json()["approval_status"] == "USED"
        assert approval_decision_response.json()["tool_call_status"] == "SUCCEEDED"
        assert approval_decision_response.json()["run_state"] == "COMPLETE"
        repeated_decision_responses = [
            client.post(
                f"/api/v1/approvals/{approved_approval_id}/decision",
                json={"decision": "APPROVE", "decision_token": approval_token},
                headers=approver_headers,
            )
            for _ in range(10)
        ]
        for repeated_decision_response in repeated_decision_responses:
            _expect(repeated_decision_response, 409)

        cancelled_candidate = app.state.agent_runner.create(
            support,
            AgentRunCreateInput(
                message="create ticket",
                context={
                    "customer_id": customer_id,
                    "title": "Cancelled smoke ticket",
                    "description": "This pending write must never start.",
                },
            ),
        )
        while app.state.agent_runner.get(support, cancelled_candidate.id).state != "EXECUTE":
            app.state.agent_runner.advance_once(support, cancelled_candidate.id)
        cancelled_response = client.post(
            f"/api/v1/agent-runs/{cancelled_candidate.id}/cancel",
            headers=support_headers,
        )
        _expect(cancelled_response, 200)
        assert cancelled_response.json()["state"] == "CANCELLED"
        assert (
            app.state.agent_runner.run_to_pause(support, cancelled_candidate.id).state
            == "CANCELLED"
        )

        restart_candidate = app.state.agent_runner.create(
            support,
            AgentRunCreateInput(
                message="search knowledge",
                context={"query": "refund eligibility"},
            ),
        )
        restart_checkpoint = app.state.agent_runner.advance_once(
            support,
            restart_candidate.id,
        )
        assert restart_checkpoint.state == "CLASSIFY"

        direct_refund_response = client.post(
            "/api/v1/refunds",
            json=refund_arguments,
            headers={
                **admin_headers,
                "X-Workflow-Run-ID": str(admin_run),
                "Idempotency-Key": f"smoke-refund-direct-{suffix}",
            },
        )
        _expect(direct_refund_response, 202)
        assert direct_refund_response.json()["status"] == "APPROVAL_REQUIRED"
        direct_approval_id = direct_refund_response.json()["approval_id"]
        direct_token_response = client.post(
            f"/api/v1/approvals/{direct_approval_id}/decision-token",
            headers=approver_headers,
        )
        _expect(direct_token_response, 201)
        direct_decision_response = client.post(
            f"/api/v1/approvals/{direct_approval_id}/decision",
            json={
                "decision": "APPROVE",
                "decision_token": direct_token_response.json()["decision_token"],
            },
            headers=approver_headers,
        )
        _expect(direct_decision_response, 200)
        assert direct_decision_response.json()["origin"] == "DIRECT_API"
        assert direct_decision_response.json()["run_state"] == "RECEIVED"

        schemas_response = client.get("/api/v1/tools/schemas", headers=admin_headers)
        _expect(schemas_response, 200)
        assert len(schemas_response.json()["tools"]) == 8
        assert client.post("/api/v1/tickets", json={}).status_code == 401

    app.state.engine.dispose()
    restarted_app = create_app(settings)
    resumed_after_restart = restarted_app.state.agent_runner.run_to_pause(
        support,
        restart_candidate.id,
    )
    assert resumed_after_restart.state == "COMPLETE"
    restarted_app.state.engine.dispose()

    session_factory = app.state.session_factory
    with session_factory() as session:
        ticket_count = session.scalar(
            select(func.count()).select_from(Ticket).where(Ticket.id == UUID(ticket_id))
        )
        approval_id = UUID(approval_response.json()["approval_id"])
        approval_count = session.scalar(
            select(func.count()).select_from(Approval).where(Approval.id == approval_id)
        )
        refund_id = UUID(direct_decision_response.json()["result"]["id"])
        refund_count = session.scalar(
            select(func.count()).select_from(Refund).where(Refund.id == refund_id)
        )
        ticket_call_count = session.scalar(
            select(func.count()).select_from(ToolCall).where(
                ToolCall.tool_name == "create_ticket",
                ToolCall.idempotency_key == create_ticket_request["idempotency_key"],
            )
        )
        assert ticket_count == approval_count == refund_count == ticket_call_count == 1
        assert session.scalar(
            select(func.count()).select_from(WorkflowRun).where(
                WorkflowRun.id.in_(
                    [knowledge_agent_id, refund_agent_id, approved_agent_id]
                )
            )
        ) == 3
        for agent_run_id in (
            knowledge_agent_id,
            refund_agent_id,
            approved_agent_id,
        ):
            persisted_run = session.get(WorkflowRun, agent_run_id)
            assert persisted_run is not None
            assert session.scalar(
                select(func.count()).select_from(WorkflowCheckpoint).where(
                    WorkflowCheckpoint.run_id == agent_run_id
                )
            ) == persisted_run.version
        assert session.scalar(
            select(func.count()).select_from(WorkflowEvent).where(
                WorkflowEvent.run_id == knowledge_agent_id
            )
        ) == 3
        assert session.scalar(
            select(func.count()).select_from(Approval).where(
                Approval.run_id == refund_agent_id
            )
        ) == 1
        ticket_call = session.scalar(
            select(ToolCall).where(
                ToolCall.tool_name == "create_ticket",
                ToolCall.idempotency_key == create_ticket_request["idempotency_key"],
            )
        )
        assert ticket_call is not None
        ticket_outbox = session.scalar(
            select(SideEffectOutbox).where(
                SideEffectOutbox.tool_call_id == ticket_call.id
            )
        )
        assert ticket_outbox is not None
        assert ticket_outbox.status == "SUCCEEDED"
        assert ticket_outbox.attempts == 1
        approved_call = session.scalar(
            select(ToolCall).where(ToolCall.approval_id == approved_approval_id)
        )
        assert approved_call is not None
        approved_outbox = session.scalar(
            select(SideEffectOutbox).where(
                SideEffectOutbox.tool_call_id == approved_call.id
            )
        )
        assert approved_outbox is not None
        assert approved_outbox.status == "SUCCEEDED"
        assert session.scalar(
            select(func.count()).select_from(Ticket).where(
                Ticket.title == "Cancelled smoke ticket"
            )
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(SideEffectOutbox).where(
                SideEffectOutbox.run_id == cancelled_candidate.id
            )
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(Refund).where(
                Refund.order_id == approved_arguments["order_id"]
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(ToolCall).where(
                ToolCall.approval_id == approved_approval_id,
                ToolCall.status == "SUCCEEDED",
            )
        ) == 1

    print(
        json.dumps(
            {
                "status": "ok",
                "tool_schemas": 8,
                "ticket_replays": 10,
                "ticket_side_effects": 1,
                "high_risk_tool": "APPROVAL_REQUIRED",
                "direct_refund_api": "APPROVED_THEN_ISSUED",
                "knowledge_agent": "COMPLETE",
                "knowledge_agent_events": 3,
                "approval_approved_agent": "COMPLETE",
                "approval_rejected_agent": "NON_RETRYABLE_FAILURE",
                "decision_token_single_use": True,
                "refund_decision_replays": 10,
                "write_outbox": "SUCCEEDED",
                "cancelled_pending_write_side_effects": 0,
                "checkpoint_count_matches_version": True,
                "postgres_restart_agent": "COMPLETE",
            },
            sort_keys=True,
        )
    )


def _principal(tenant_id: UUID, role: Role) -> Principal:
    roles = frozenset({role})
    return Principal(
        user_id=uuid4(),
        tenant_id=tenant_id,
        roles=roles,
        scopes=granted_scopes(roles),
    )


def _auth_headers(principal: Principal, settings: Settings) -> dict[str, str]:
    token = create_access_token(
        principal,
        secret=settings.jwt_secret.get_secret_value(),
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
    )
    return {"Authorization": f"Bearer {token}"}


def _create_run(client: TestClient, headers: dict[str, str]) -> UUID:
    response = client.post("/api/v1/workflow-runs", json={}, headers=headers)
    _expect(response, 201)
    return UUID(response.json()["id"])


def _expect(response: object, expected_status: int) -> None:
    actual_status = response.status_code  # type: ignore[attr-defined]
    assert actual_status == expected_status, response.text  # type: ignore[attr-defined]


if __name__ == "__main__":
    main()
