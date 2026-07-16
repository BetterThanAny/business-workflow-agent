"""Add M2 structured workflow checkpoints and events.

Revision ID: 20260713_0002
Revises: 20260713_0001
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260713_0002"
down_revision: str | None = "20260713_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workflow_run",
        sa.Column("message", sa.Text(), server_default="", nullable=False),
    )
    op.add_column(
        "workflow_run",
        sa.Column("context", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
    )
    op.add_column("workflow_run", sa.Column("proposal", sa.JSON(), nullable=True))
    op.add_column("workflow_run", sa.Column("result_payload", sa.JSON(), nullable=True))
    op.add_column("workflow_run", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column(
        "workflow_run", sa.Column("error_code", sa.String(length=100), nullable=True)
    )
    op.add_column(
        "workflow_run",
        sa.Column("pending_fields", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
    )
    op.add_column(
        "workflow_run",
        sa.Column(
            "validation_errors", sa.JSON(), server_default=sa.text("'[]'"), nullable=False
        ),
    )
    for column_name in (
        "step_count",
        "tool_call_count",
        "tokens_used",
        "cost_cents_used",
        "schema_repair_attempts",
        "event_sequence",
    ):
        op.add_column(
            "workflow_run",
            sa.Column(column_name, sa.Integer(), server_default="0", nullable=False),
        )

    op.add_column(
        "audit_event",
        sa.Column(
            "state", sa.String(length=32), server_default="RECEIVED", nullable=False
        ),
    )

    op.create_table(
        "workflow_checkpoint",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("snapshot_redacted", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_run.id"],
            name="fk_workflow_checkpoint_run_id_workflow_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workflow_checkpoint"),
        sa.UniqueConstraint(
            "run_id", "version", name="uq_workflow_checkpoint_run_id"
        ),
    )
    op.create_index(
        "ix_workflow_checkpoint_tenant_id", "workflow_checkpoint", ["tenant_id"]
    )
    op.create_index(
        "ix_workflow_checkpoint_user_id", "workflow_checkpoint", ["user_id"]
    )
    op.create_index(
        "ix_workflow_checkpoint_run_created",
        "workflow_checkpoint",
        ["run_id", "created_at"],
    )

    op.create_table(
        "workflow_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("payload_redacted", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_run.id"],
            name="fk_workflow_event_run_id_workflow_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workflow_event"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_workflow_event_run_id"),
    )
    op.create_index("ix_workflow_event_tenant_id", "workflow_event", ["tenant_id"])
    op.create_index("ix_workflow_event_user_id", "workflow_event", ["user_id"])
    op.create_index(
        "ix_workflow_event_run_created", "workflow_event", ["run_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("workflow_event")
    op.drop_table("workflow_checkpoint")
    op.drop_column("audit_event", "state")
    for column_name in (
        "event_sequence",
        "schema_repair_attempts",
        "cost_cents_used",
        "tokens_used",
        "tool_call_count",
        "step_count",
        "validation_errors",
        "pending_fields",
        "error_code",
        "summary",
        "result_payload",
        "proposal",
        "context",
        "message",
    ):
        op.drop_column("workflow_run", column_name)

