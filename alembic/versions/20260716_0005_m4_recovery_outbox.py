"""Add M4 durable retry, cancellation, and write outbox state.

Revision ID: 20260716_0005
Revises: 20260715_0004
Create Date: 2026-07-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260716_0005"
down_revision: str | None = "20260715_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workflow_run",
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "workflow_run", sa.Column("retry_from_state", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "workflow_run", sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "workflow_run",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "side_effect_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("tool_call_id", sa.Uuid(), nullable=False),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_redacted", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_run.id"],
            name="fk_side_effect_outbox_run_id_workflow_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tool_call_id"],
            ["tool_call.id"],
            name="fk_side_effect_outbox_tool_call_id_tool_call",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_side_effect_outbox"),
        sa.UniqueConstraint(
            "tool_call_id", name="uq_side_effect_outbox_tool_call_id"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "tool_name",
            "idempotency_key",
            name="uq_side_effect_outbox_tenant_tool_idempotency",
        ),
    )
    op.create_index(
        "ix_side_effect_outbox_tenant_id", "side_effect_outbox", ["tenant_id"]
    )
    op.create_index(
        "ix_side_effect_outbox_user_id", "side_effect_outbox", ["user_id"]
    )
    op.create_index(
        "ix_side_effect_outbox_status_available",
        "side_effect_outbox",
        ["status", "available_at"],
    )

def downgrade() -> None:
    op.drop_table("side_effect_outbox")
    op.drop_column("workflow_run", "cancel_requested_at")
    op.drop_column("workflow_run", "next_retry_at")
    op.drop_column("workflow_run", "retry_from_state")
    op.drop_column("workflow_run", "retry_count")
