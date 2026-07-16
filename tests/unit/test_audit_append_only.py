from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from business_workflow_agent.models import (
    AuditEvent,
    SideEffectEvent,
    SideEffectOutbox,
    ToolCall,
    WorkflowRun,
)


def test_audit_event_cannot_be_updated_or_deleted(session: Session) -> None:
    run = WorkflowRun(
        tenant_id=uuid4(),
        user_id=uuid4(),
        budget={"max_steps": 1},
    )
    session.add(run)
    session.flush()
    audit = AuditEvent(
        tenant_id=run.tenant_id,
        user_id=run.user_id,
        run_id=run.id,
        tool_name="test",
        event_type="CREATED",
        payload_redacted={},
    )
    session.add(audit)
    session.commit()

    audit.event_type = "CHANGED"
    with pytest.raises(ValueError, match="append-only"):
        session.commit()
    session.rollback()

    session.delete(audit)
    with pytest.raises(ValueError, match="append-only"):
        session.commit()


def test_side_effect_event_cannot_be_updated_or_deleted(session: Session) -> None:
    run = WorkflowRun(
        tenant_id=uuid4(),
        user_id=uuid4(),
        budget={"max_steps": 1},
    )
    session.add(run)
    session.flush()
    call = ToolCall(
        tenant_id=run.tenant_id,
        user_id=run.user_id,
        run_id=run.id,
        tool_name="create_ticket",
        tool_version="1.0.0",
        risk_class="WRITE_LOW_RISK",
        idempotency_key="append-only-event",
        request_hash="0" * 64,
        arguments_redacted={},
    )
    session.add(call)
    session.flush()
    outbox = SideEffectOutbox(
        tenant_id=run.tenant_id,
        user_id=run.user_id,
        run_id=run.id,
        tool_call_id=call.id,
        tool_name=call.tool_name,
        idempotency_key=call.idempotency_key,
        payload={},
        status="PENDING",
    )
    session.add(outbox)
    session.flush()
    side_effect_event = SideEffectEvent(
        tenant_id=run.tenant_id,
        user_id=run.user_id,
        run_id=run.id,
        tool_call_id=call.id,
        outbox_id=outbox.id,
        sequence=1,
        event_type="OUTBOX_ENQUEUED",
        payload_redacted={},
    )
    session.add(side_effect_event)
    session.commit()

    side_effect_event.event_type = "CHANGED"
    with pytest.raises(ValueError, match="append-only"):
        session.commit()
    session.rollback()

    session.delete(side_effect_event)
    with pytest.raises(ValueError, match="append-only"):
        session.commit()
