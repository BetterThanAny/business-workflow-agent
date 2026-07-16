from collections.abc import Callable
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import Engine

from business_workflow_agent.app import create_app
from business_workflow_agent.auth import Principal, Role
from business_workflow_agent.config import Settings
from business_workflow_agent.observability import WorkflowTelemetry


def test_trajectory_unifies_redacted_state_tool_side_effect_and_error_evidence(
    client: TestClient,
    principal_factory: Callable[..., Principal],
    auth_headers: Callable[[Principal], dict[str, str]],
    create_customer: Callable[[Principal, str], dict[str, object]],
) -> None:
    admin = principal_factory(Role.ADMIN)
    support = principal_factory(Role.SUPPORT_AGENT)
    customer = create_customer(admin, "m5-trajectory")
    marker = "private-trajectory-marker"
    started = client.post(
        "/api/v1/agent-runs",
        json={
            "message": "create ticket",
            "context": {
                "customer_id": customer["id"],
                "title": "Cannot sign in",
                "description": marker,
            },
        },
        headers=auth_headers(support),
    )
    assert started.status_code == 201

    trajectory = client.get(
        f"/api/v1/agent-runs/{started.json()['id']}/trajectory",
        headers=auth_headers(support),
    )

    assert trajectory.status_code == 200, trajectory.text
    payload = trajectory.json()
    assert payload["state"] == "COMPLETE"
    kinds = {item["kind"] for item in payload["items"]}
    assert {"checkpoint", "workflow_event", "tool_call", "side_effect", "audit"} <= kinds
    assert marker not in trajectory.text
    assert "[REDACTED]" in trajectory.text

    peer = principal_factory(Role.SUPPORT_AGENT)
    peer_denied = client.get(
        f"/api/v1/agent-runs/{started.json()['id']}/trajectory",
        headers=auth_headers(peer),
    )
    assert peer_denied.status_code == 404

    auditor = principal_factory(Role.AUDITOR)
    auditor_view = client.get(
        f"/api/v1/agent-runs/{started.json()['id']}/trajectory",
        headers=auth_headers(auditor),
    )
    assert auditor_view.status_code == 200

    foreign = Principal(
        user_id=uuid4(),
        tenant_id=uuid4(),
        roles=frozenset({Role.ADMIN}),
        scopes=frozenset({"workflow:create"}),
    )
    denied = client.get(
        f"/api/v1/agent-runs/{started.json()['id']}/trajectory",
        headers=auth_headers(foreign),
    )
    assert denied.status_code == 404


def test_prometheus_metrics_and_workflow_tool_llm_spans_are_emitted(
    engine: Engine,
    settings: Settings,
    principal_factory: Callable[..., Principal],
    auth_headers: Callable[[Principal], dict[str, str]],
) -> None:
    exporter = InMemorySpanExporter()
    telemetry = WorkflowTelemetry(span_exporter=exporter)
    support = principal_factory(Role.SUPPORT_AGENT)
    with TestClient(create_app(settings, engine=engine, telemetry=telemetry)) as client:
        response = client.post(
            "/api/v1/agent-runs",
            json={
                "message": "search knowledge",
                "context": {"query": "reset MFA"},
            },
            headers=auth_headers(support),
        )
        metrics = client.get("/metrics")

    assert response.status_code == 201
    assert metrics.status_code == 200
    assert 'business_workflow_runs_created_total{role="SUPPORT_AGENT"} 1.0' in metrics.text
    assert "business_workflow_state_transitions_total" in metrics.text
    assert "business_workflow_tool_executions_total" in metrics.text
    assert "business_workflow_orchestration_duration_milliseconds" in metrics.text
    span_names = {span.name for span in exporter.get_finished_spans()}
    expected_spans = {
        "workflow.run",
        "workflow.step",
        "llm.classify",
        "llm.summarize",
        "tool.execute",
    }
    assert expected_spans <= span_names


def test_one_trajectory_shows_approval_tool_and_terminal_error(
    client: TestClient,
    principal_factory: Callable[..., Principal],
    auth_headers: Callable[[Principal], dict[str, str]],
    create_run: Callable[[Principal], UUID],
) -> None:
    requester = principal_factory(Role.REFUND_MANAGER)
    approver = principal_factory(Role.ADMIN)
    requester_headers = auth_headers(requester)
    business_run = create_run(requester)
    quote_request = {
        "order_id": f"m5-trajectory-{uuid4().hex}",
        "purchase_amount_cents": 5000,
        "requested_amount_cents": 500,
        "currency": "CNY",
        "reason": "M5 terminal trajectory",
    }
    quote = client.post(
        "/api/v1/refunds/quote",
        json=quote_request,
        headers={**requester_headers, "X-Workflow-Run-ID": str(business_run)},
    )
    assert quote.status_code == 200
    started = client.post(
        "/api/v1/agent-runs",
        json={
            "message": "issue refund",
            "context": {
                "quote_id": quote.json()["quote_id"],
                "order_id": quote_request["order_id"],
                "purchase_amount_cents": quote_request["purchase_amount_cents"],
                "amount_cents": quote_request["requested_amount_cents"],
                "currency": quote_request["currency"],
                "reason": quote_request["reason"],
            },
        },
        headers=requester_headers,
    )
    approval_id = UUID(started.json()["result"]["approval_id"])
    issued = client.post(
        f"/api/v1/approvals/{approval_id}/decision-token",
        headers=auth_headers(approver),
    )
    rejected = client.post(
        f"/api/v1/approvals/{approval_id}/decision",
        json={"decision": "REJECT", "decision_token": issued.json()["decision_token"]},
        headers=auth_headers(approver),
    )
    assert rejected.status_code == 200

    response = client.get(
        f"/api/v1/agent-runs/{started.json()['id']}/trajectory",
        headers=requester_headers,
    )

    assert response.status_code == 200
    trajectory = response.json()
    assert trajectory["state"] == "NON_RETRYABLE_FAILURE"
    assert trajectory["error_code"] == "APPROVAL_REJECTED"
    kinds = {item["kind"] for item in trajectory["items"]}
    assert {"checkpoint", "workflow_event", "approval", "tool_call", "audit"} <= kinds
    assert any(item["error_code"] == "APPROVAL_REJECTED" for item in trajectory["items"])
    assert any(
        item["kind"] == "approval" and item["status"] == "REJECTED"
        for item in trajectory["items"]
    )
