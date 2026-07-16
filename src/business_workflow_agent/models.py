from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from business_workflow_agent.db import Base
from business_workflow_agent.domain import (
    ApprovalStatus,
    OutboxStatus,
    RefundStatus,
    TicketPriority,
    TicketStatus,
    ToolCallStatus,
    WorkflowState,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class Customer(TimestampMixin, Base):
    __tablename__ = "customer"
    __table_args__ = (UniqueConstraint("tenant_id", "external_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    external_id: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)

    tickets: Mapped[list["Ticket"]] = relationship(back_populates="customer")


class Ticket(TimestampMixin, Base):
    __tablename__ = "ticket"
    __table_args__ = (Index("ix_ticket_tenant_customer", "tenant_id", "customer_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    customer_id: Mapped[UUID] = mapped_column(
        ForeignKey("customer.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(
        String(16), default=TicketPriority.NORMAL.value, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default=TicketStatus.OPEN.value, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    customer: Mapped[Customer] = relationship(back_populates="tickets")
    events: Mapped[list["TicketEvent"]] = relationship(back_populates="ticket")


class TicketEvent(Base):
    __tablename__ = "ticket_event"
    __table_args__ = (Index("ix_ticket_event_ticket_created", "ticket_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    ticket_id: Mapped[UUID] = mapped_column(
        ForeignKey("ticket.id", ondelete="RESTRICT"), nullable=False
    )
    actor_user_id: Mapped[UUID] = mapped_column(nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    event_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    ticket: Mapped[Ticket] = relationship(back_populates="events")


class WorkflowRun(TimestampMixin, Base):
    __tablename__ = "workflow_run"
    __table_args__ = (Index("ix_workflow_run_tenant_user", "tenant_id", "user_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    user_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), default=WorkflowState.RECEIVED.value, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    budget: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    context_data: Mapped[dict[str, Any]] = mapped_column(
        "context", JSON, default=dict, nullable=False
    )
    proposal: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    pending_fields: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    validation_errors: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    step_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_cents_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    schema_repair_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    event_sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retry_from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class WorkflowCheckpoint(Base):
    __tablename__ = "workflow_checkpoint"
    __table_args__ = (
        UniqueConstraint("run_id", "version"),
        Index("ix_workflow_checkpoint_run_created", "run_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    user_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("workflow_run.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot_redacted: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class WorkflowEvent(Base):
    __tablename__ = "workflow_event"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence"),
        Index("ix_workflow_event_run_created", "run_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    user_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("workflow_run.id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload_redacted: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class Approval(TimestampMixin, Base):
    __tablename__ = "approval"
    __table_args__ = (Index("ix_approval_tenant_status", "tenant_id", "status"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("workflow_run.id", ondelete="RESTRICT"), nullable=False
    )
    requested_by_user_id: Mapped[UUID] = mapped_column(nullable=False)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    tool_arguments: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    tool_arguments_available: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    tool_arguments_redacted: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default=ApprovalStatus.PENDING.value, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_by_user_id: Mapped[UUID | None] = mapped_column(nullable=True)
    decision_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decision_token_issued_to_user_id: Mapped[UUID | None] = mapped_column(nullable=True)
    decision_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decision_token_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ToolCall(Base):
    __tablename__ = "tool_call"
    __table_args__ = (
        UniqueConstraint("tenant_id", "tool_name", "idempotency_key"),
        Index("uq_tool_call_approval_id", "approval_id", unique=True),
        Index("ix_tool_call_run_created", "run_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    user_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("workflow_run.id", ondelete="RESTRICT"), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(20), nullable=False)
    risk_class: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments_redacted: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=ToolCallStatus.IN_PROGRESS.value, nullable=False
    )
    result_redacted: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    approval_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("approval.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SideEffectOutbox(TimestampMixin, Base):
    __tablename__ = "side_effect_outbox"
    __table_args__ = (
        UniqueConstraint("tool_call_id"),
        UniqueConstraint(
            "tenant_id",
            "tool_name",
            "idempotency_key",
            name="uq_side_effect_outbox_tenant_tool_idempotency",
        ),
        Index("ix_side_effect_outbox_status_available", "status", "available_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    user_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("workflow_run.id", ondelete="RESTRICT"), nullable=False
    )
    tool_call_id: Mapped[UUID] = mapped_column(
        ForeignKey("tool_call.id", ondelete="RESTRICT"), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=OutboxStatus.PENDING.value, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    event_sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    result_redacted: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)


class SideEffectEvent(Base):
    __tablename__ = "side_effect_event"
    __table_args__ = (
        UniqueConstraint("outbox_id", "sequence"),
        Index("ix_side_effect_event_outbox_created", "outbox_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    user_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("workflow_run.id", ondelete="RESTRICT"), nullable=False
    )
    tool_call_id: Mapped[UUID] = mapped_column(
        ForeignKey("tool_call.id", ondelete="RESTRICT"), nullable=False
    )
    outbox_id: Mapped[UUID] = mapped_column(
        ForeignKey("side_effect_outbox.id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload_redacted: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AuditEvent(Base):
    __tablename__ = "audit_event"
    __table_args__ = (Index("ix_audit_event_run_created", "run_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    user_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("workflow_run.id", ondelete="RESTRICT"), nullable=False
    )
    tool_call_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tool_call.id", ondelete="RESTRICT"), nullable=True
    )
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), default=WorkflowState.RECEIVED.value, nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload_redacted: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class Refund(Base):
    __tablename__ = "refund"
    __table_args__ = (Index("ix_refund_tenant_order", "tenant_id", "order_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    order_id: Mapped[str] = mapped_column(String(100), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default=RefundStatus.ISSUED.value, nullable=False
    )
    issued_by_user_id: Mapped[UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


@event.listens_for(AuditEvent, "before_update", propagate=True)
def _prevent_audit_update(_mapper: object, _connection: object, _target: AuditEvent) -> None:
    raise ValueError("audit events are append-only")


@event.listens_for(AuditEvent, "before_delete", propagate=True)
def _prevent_audit_delete(_mapper: object, _connection: object, _target: AuditEvent) -> None:
    raise ValueError("audit events are append-only")


@event.listens_for(SideEffectEvent, "before_update", propagate=True)
def _prevent_side_effect_event_update(
    _mapper: object, _connection: object, _target: SideEffectEvent
) -> None:
    raise ValueError("side-effect events are append-only")


@event.listens_for(SideEffectEvent, "before_delete", propagate=True)
def _prevent_side_effect_event_delete(
    _mapper: object, _connection: object, _target: SideEffectEvent
) -> None:
    raise ValueError("side-effect events are append-only")
