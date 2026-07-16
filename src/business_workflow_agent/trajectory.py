from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from business_workflow_agent.auth import Principal, Role
from business_workflow_agent.domain import WorkflowState
from business_workflow_agent.models import (
    Approval,
    AuditEvent,
    SideEffectEvent,
    ToolCall,
    WorkflowCheckpoint,
    WorkflowEvent,
    WorkflowRun,
)
from business_workflow_agent.schemas import RunTrajectoryItem, RunTrajectoryOutput
from business_workflow_agent.services import ResourceNotFound


class RunTrajectoryService:
    """Build one tenant-scoped, redacted trajectory from persisted evidence."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, principal: Principal, run_id: UUID) -> RunTrajectoryOutput:
        run = self.session.get(WorkflowRun, run_id)
        if run is None or run.tenant_id != principal.tenant_id:
            raise ResourceNotFound("workflow run not found")
        privileged = bool(principal.roles.intersection({Role.AUDITOR, Role.ADMIN}))
        if run.user_id != principal.user_id and not privileged:
            raise ResourceNotFound("workflow run not found")

        items: list[RunTrajectoryItem] = []
        checkpoints = self.session.scalars(
            select(WorkflowCheckpoint)
            .where(WorkflowCheckpoint.run_id == run_id)
            .order_by(WorkflowCheckpoint.version)
        )
        for checkpoint in checkpoints:
            items.append(
                RunTrajectoryItem(
                    kind="checkpoint",
                    occurred_at=checkpoint.created_at,
                    state=checkpoint.state,
                    error_code=checkpoint.snapshot_redacted.get("error_code"),
                    details=checkpoint.snapshot_redacted,
                )
            )

        events = self.session.scalars(
            select(WorkflowEvent)
            .where(WorkflowEvent.run_id == run_id)
            .order_by(WorkflowEvent.sequence)
        )
        for event in events:
            items.append(
                RunTrajectoryItem(
                    kind="workflow_event",
                    occurred_at=event.created_at,
                    status=event.event_type,
                    details={"sequence": event.sequence, **event.payload_redacted},
                )
            )

        approvals = self.session.scalars(
            select(Approval).where(Approval.run_id == run_id).order_by(Approval.created_at)
        )
        for approval in approvals:
            items.append(
                RunTrajectoryItem(
                    kind="approval",
                    occurred_at=approval.created_at,
                    status=approval.status,
                    tool_name=approval.tool_name,
                    details={
                        "approval_id": str(approval.id),
                        "arguments": approval.tool_arguments_redacted,
                        "expires_at": approval.expires_at.isoformat(),
                        "decided_at": (
                            approval.decided_at.isoformat()
                            if approval.decided_at is not None
                            else None
                        ),
                    },
                )
            )

        tool_calls = self.session.scalars(
            select(ToolCall).where(ToolCall.run_id == run_id).order_by(ToolCall.created_at)
        )
        for call in tool_calls:
            items.append(
                RunTrajectoryItem(
                    kind="tool_call",
                    occurred_at=call.created_at,
                    status=call.status,
                    tool_name=call.tool_name,
                    error_code=call.error_code,
                    details={
                        "tool_call_id": str(call.id),
                        "tool_version": call.tool_version,
                        "risk_class": call.risk_class,
                        "idempotency_key": call.idempotency_key,
                        "arguments": call.arguments_redacted,
                        "result": call.result_redacted,
                        "approval_id": str(call.approval_id) if call.approval_id else None,
                    },
                )
            )

        side_effects = self.session.scalars(
            select(SideEffectEvent)
            .where(SideEffectEvent.run_id == run_id)
            .order_by(SideEffectEvent.created_at, SideEffectEvent.sequence)
        )
        for event in side_effects:
            items.append(
                RunTrajectoryItem(
                    kind="side_effect",
                    occurred_at=event.created_at,
                    status=event.event_type,
                    details={
                        "tool_call_id": str(event.tool_call_id),
                        "outbox_id": str(event.outbox_id),
                        "sequence": event.sequence,
                        **event.payload_redacted,
                    },
                )
            )

        audits = self.session.scalars(
            select(AuditEvent)
            .where(AuditEvent.run_id == run_id)
            .order_by(AuditEvent.created_at)
        )
        for event in audits:
            items.append(
                RunTrajectoryItem(
                    kind="audit",
                    occurred_at=event.created_at,
                    state=event.state,
                    status=event.event_type,
                    tool_name=event.tool_name,
                    details=event.payload_redacted,
                )
            )

        items.sort(key=lambda item: item.occurred_at)
        return RunTrajectoryOutput(
            run_id=run.id,
            state=WorkflowState(run.state),
            version=run.version,
            error_code=run.error_code,
            items=items,
        )
