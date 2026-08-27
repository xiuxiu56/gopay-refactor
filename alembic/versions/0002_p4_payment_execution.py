"""增加 P4 支付执行与远端核验字段。

Revision ID: 0002_p4_payment_execution
Revises: 0001_p0_initial
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002_p4_payment_execution"
down_revision = "0001_p0_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payment_intents", sa.Column("task_id", sa.String(length=36), nullable=True))
    op.add_column(
        "payment_intents",
        sa.Column("amount", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "payment_intents",
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="IDR"),
    )
    op.add_column(
        "payment_intents",
        sa.Column("transaction_status", sa.String(length=32), nullable=False, server_default=""),
    )
    op.add_column(
        "payment_intents",
        sa.Column("last_error_message", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("idx_payment_intents_task", "payment_intents", ["task_id"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_payment_intents_task", table_name="payment_intents")
    op.drop_column("payment_intents", "last_error_message")
    op.drop_column("payment_intents", "transaction_status")
    op.drop_column("payment_intents", "currency")
    op.drop_column("payment_intents", "amount")
    op.drop_column("payment_intents", "task_id")
