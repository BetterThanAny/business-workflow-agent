"""Create the M1 business and tool execution schema.

Revision ID: 20260713_0001
Revises: None
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260713_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> tuple[sa.Column[object], sa.Column[object]]:
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "customer",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_customer"),
        sa.UniqueConstraint("tenant_id", "external_id", name="uq_customer_tenant_id"),
    )
    op.create_index("ix_customer_tenant_id", "customer", ["tenant_id"])

    op.create_table(
        "workflow_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("budget", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_workflow_run"),
    )
    op.create_index("ix_workflow_run_tenant_id", "workflow_run", ["tenant_id"])
    op.create_index("ix_workflow_run_user_id", "workflow_run", ["user_id"])
    op.create_index(
        "ix_workflow_run_tenant_user", "workflow_run", ["tenant_id", "user_id"]
    )

    op.create_table(
        "ticket",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("priority", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customer.id"],
            name="fk_ticket_customer_id_customer",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ticket"),
    )
    op.create_index("ix_ticket_tenant_id", "ticket", ["tenant_id"])
    op.create_index("ix_ticket_tenant_customer", "ticket", ["tenant_id", "customer_id"])

    op.create_table(
        "approval",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        sa.Column("tool_arguments_redacted", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("decision_token_hash", sa.String(length=128), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_run.id"],
            name="fk_approval_run_id_workflow_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_approval"),
    )
    op.create_index("ix_approval_tenant_id", "approval", ["tenant_id"])
    op.create_index("ix_approval_tenant_status", "approval", ["tenant_id", "status"])

    op.create_table(
        "refund",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.String(length=100), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("issued_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_refund"),
    )
    op.create_index("ix_refund_tenant_id", "refund", ["tenant_id"])
    op.create_index("ix_refund_tenant_order", "refund", ["tenant_id", "order_id"])

    op.create_table(
        "ticket_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("ticket_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("event_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["ticket.id"],
            name="fk_ticket_event_ticket_id_ticket",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ticket_event"),
    )
    op.create_index("ix_ticket_event_tenant_id", "ticket_event", ["tenant_id"])
    op.create_index(
        "ix_ticket_event_ticket_created", "ticket_event", ["ticket_id", "created_at"]
    )

    op.create_table(
        "tool_call",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        sa.Column("tool_version", sa.String(length=20), nullable=False),
        sa.Column("risk_class", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("arguments_redacted", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result_redacted", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("approval_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["approval.id"],
            name="fk_tool_call_approval_id_approval",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_run.id"],
            name="fk_tool_call_run_id_workflow_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tool_call"),
        sa.UniqueConstraint(
            "tenant_id",
            "tool_name",
            "idempotency_key",
            name="uq_tool_call_tenant_id",
        ),
    )
    op.create_index("ix_tool_call_tenant_id", "tool_call", ["tenant_id"])
    op.create_index("ix_tool_call_user_id", "tool_call", ["user_id"])
    op.create_index("ix_tool_call_run_created", "tool_call", ["run_id", "created_at"])

    op.create_table(
        "audit_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("tool_call_id", sa.Uuid(), nullable=True),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload_redacted", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_run.id"],
            name="fk_audit_event_run_id_workflow_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tool_call_id"],
            ["tool_call.id"],
            name="fk_audit_event_tool_call_id_tool_call",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_event"),
    )
    op.create_index("ix_audit_event_tenant_id", "audit_event", ["tenant_id"])
    op.create_index("ix_audit_event_user_id", "audit_event", ["user_id"])
    op.create_index(
        "ix_audit_event_run_created", "audit_event", ["run_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("audit_event")
    op.drop_table("tool_call")
    op.drop_table("ticket_event")
    op.drop_table("refund")
    op.drop_table("approval")
    op.drop_table("ticket")
    op.drop_table("workflow_run")
    op.drop_table("customer")
