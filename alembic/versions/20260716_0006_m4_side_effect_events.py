"""Add M4 append-only side-effect trajectory events.

Revision ID: 20260716_0006
Revises: 20260716_0005
Create Date: 2026-07-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260716_0006"
down_revision: str | None = "20260716_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "side_effect_outbox",
        sa.Column("event_sequence", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_table(
        "side_effect_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("tool_call_id", sa.Uuid(), nullable=False),
        sa.Column("outbox_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("payload_redacted", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_run.id"],
            name="fk_side_effect_event_run_id_workflow_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tool_call_id"],
            ["tool_call.id"],
            name="fk_side_effect_event_tool_call_id_tool_call",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["outbox_id"],
            ["side_effect_outbox.id"],
            name="fk_side_effect_event_outbox_id_side_effect_outbox",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_side_effect_event"),
        sa.UniqueConstraint(
            "outbox_id", "sequence", name="uq_side_effect_event_outbox_id"
        ),
    )
    op.create_index(
        "ix_side_effect_event_tenant_id", "side_effect_event", ["tenant_id"]
    )
    op.create_index(
        "ix_side_effect_event_user_id", "side_effect_event", ["user_id"]
    )
    op.create_index(
        "ix_side_effect_event_outbox_created",
        "side_effect_event",
        ["outbox_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("side_effect_event")
    op.drop_column("side_effect_outbox", "event_sequence")
