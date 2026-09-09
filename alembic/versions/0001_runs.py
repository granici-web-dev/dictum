"""Таблица runs: состояние прогона переезжает из памяти процесса в базу.

Revision ID: 0001_runs
Revises:
Create Date: 2026-09-09
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0001_runs"
down_revision: str | None = None
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column("lang", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("auto_approve", sa.Boolean(), nullable=False),
        sa.Column("stopped_stage", sa.String(16), nullable=True),
        sa.Column("stopped_artifact", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    )
    op.create_index("ix_runs_chat_status", "runs", ["chat_id", "status"])
    # Частичный уникальный индекс, а не проверка в коде: остановка в чате одна, и вторая
    # означала бы, что ответ человека уходит непонятно в какой прогон.
    op.create_index(
        "ux_runs_one_stop_per_chat",
        "runs",
        ["chat_id"],
        unique=True,
        postgresql_where=sa.text("status = 'awaiting_choice'"),
    )


def downgrade() -> None:
    op.drop_index("ux_runs_one_stop_per_chat", table_name="runs")
    op.drop_index("ix_runs_chat_status", table_name="runs")
    op.drop_table("runs")
