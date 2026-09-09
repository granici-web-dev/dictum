"""Ходы brief-диалога в строке прогона и третья остановка того же рода.

Revision ID: 0003_brief_dialog
Revises: 0002_gate_stop
Create Date: 2026-09-09
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0003_brief_dialog"
down_revision: str | None = "0002_gate_stop"
branch_labels: None = None
depends_on: None = None

STOPS_NOW = "status in ('awaiting_answer', 'awaiting_choice', 'awaiting_gate')"
STOPS_BEFORE = "status in ('awaiting_choice', 'awaiting_gate')"


def upgrade() -> None:
    # Ответы человека на вопросы брифа не лежат ни в одном артефакте, а стадия обязана видеть
    # их все сразу: сообщение в чате прогон не переживёт, а строка переживёт (SPEC §3.3).
    op.add_column(
        "runs",
        sa.Column("brief_dialog", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    # Вопрос брифа — третья остановка того же рода: чат по-прежнему держит одну, иначе ответ
    # человека уходит непонятно в какой прогон. Условие индекса расширяется, а не заводится второе.
    op.drop_index("ux_runs_one_stop_per_chat", table_name="runs")
    op.create_index(
        "ux_runs_one_stop_per_chat",
        "runs",
        ["chat_id"],
        unique=True,
        postgresql_where=sa.text(STOPS_NOW),
    )


def downgrade() -> None:
    op.drop_index("ux_runs_one_stop_per_chat", table_name="runs")
    op.create_index(
        "ux_runs_one_stop_per_chat",
        "runs",
        ["chat_id"],
        unique=True,
        postgresql_where=sa.text(STOPS_BEFORE),
    )
    op.drop_column("runs", "brief_dialog")
