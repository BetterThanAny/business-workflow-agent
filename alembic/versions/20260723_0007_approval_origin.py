"""Record whether approval came from an agent tool or a direct REST API.

Revision ID: 20260723_0007
Revises: 20260716_0006
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260723_0007"
down_revision: str | None = "20260716_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("approval") as batch_op:
        batch_op.add_column(
            sa.Column(
                "origin",
                sa.String(length=20),
                server_default="AGENT_TOOL",
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            "ck_approval_origin",
            "origin IN ('AGENT_TOOL', 'DIRECT_API')",
        )


def downgrade() -> None:
    with op.batch_alter_table("approval") as batch_op:
        batch_op.drop_constraint("ck_approval_origin", type_="check")
        batch_op.drop_column("origin")
