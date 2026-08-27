"""增加账号流程滚动批次调度字段。

Revision ID: 0003_p6_rolling_account_flow
Revises: 0002_p4_payment_execution
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003_p6_rolling_account_flow"
down_revision = "0002_p4_payment_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task_batches",
        sa.Column("strategy", sa.String(length=32), nullable=False, server_default="fixed"),
    )
    op.add_column(
        "task_batches",
        sa.Column("plan_ciphertext", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "task_batches",
        sa.Column("next_sequence", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("task_batches", "next_sequence")
    op.drop_column("task_batches", "plan_ciphertext")
    op.drop_column("task_batches", "strategy")
