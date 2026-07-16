"""Add M3 approval decision payload and one-time token metadata.

Revision ID: 20260715_0003
Revises: 20260713_0002
Create Date: 2026-07-15
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260715_0003"
down_revision: str | None = "20260713_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "approval",
        sa.Column(
            "tool_arguments",
            sa.JSON(),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    op.add_column(
        "approval",
        sa.Column("decision_token_issued_to_user_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "approval",
        sa.Column("decision_token_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "approval",
        sa.Column("decision_token_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "approval",
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_tool_call_approval_id",
        "tool_call",
        ["approval_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_tool_call_approval_id", table_name="tool_call")
    op.drop_column("approval", "decided_at")
    op.drop_column("approval", "decision_token_used_at")
    op.drop_column("approval", "decision_token_expires_at")
    op.drop_column("approval", "decision_token_issued_to_user_id")
    op.drop_column("approval", "tool_arguments")
