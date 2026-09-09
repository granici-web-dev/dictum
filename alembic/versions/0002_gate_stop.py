"""Остановка на воротах: тот же индекс, что и у выбора, но на оба статуса.

Revision ID: 0002_gate_stop
Revises: 0001_runs
Create Date: 2026-09-09
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0002_gate_stop"
down_revision: str | None = "0001_runs"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    # Ворота — вторая остановка того же рода: чат по-прежнему держит одну, иначе ответ человека
    # уходит непонятно в какой прогон. Условие индекса поэтому расширяется, а не заводится второе.
    op.drop_index("ux_runs_one_stop_per_chat", table_name="runs")
    op.create_index(
        "ux_runs_one_stop_per_chat",
        "runs",
        ["chat_id"],
        unique=True,
        postgresql_where=sa.text("status in ('awaiting_choice', 'awaiting_gate')"),
    )


def downgrade() -> None:
    op.drop_index("ux_runs_one_stop_per_chat", table_name="runs")
    op.create_index(
        "ux_runs_one_stop_per_chat",
        "runs",
        ["chat_id"],
        unique=True,
        postgresql_where=sa.text("status = 'awaiting_choice'"),
    )
