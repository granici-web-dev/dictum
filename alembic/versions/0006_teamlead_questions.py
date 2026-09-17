"""Поручение ждёт ответа тимлида вне правила одной остановки: ресёрч ребёнка и сообщения вопросов.

Revision ID: 0006_teamlead_questions
Revises: 0005_child_runs
Create Date: 2026-09-17
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0006_teamlead_questions"
down_revision: str | None = "0005_child_runs"
branch_labels: None = None
depends_on: None = None

RESEARCH_OF_TASK = "(assignment IS NULL) = (research IS NULL)"
QUESTIONS_OF_TASK = (
    "(questions_message_id IS NULL AND questions_note_id IS NULL) OR assignment IS NOT NULL"
)


def upgrade() -> None:
    # Ресёрч поручения выбирается кнопкой и должен пережить парковку на дни, как auto_approve.
    op.add_column("runs", sa.Column("research", sa.Boolean(), nullable=True))
    # Дети поручения до 0006 шли без ресёрча: это факт, а не умолчание.
    op.execute("UPDATE runs SET research = false WHERE assignment IS NOT NULL")
    op.create_check_constraint("ck_runs_research_of_task", "runs", RESEARCH_OF_TASK)
    # Сообщения, ответом на которые приходят ответы тимлида: текст вопросов и перевод с кнопками.
    # BIGINT, как chat_id: одинаковый тип у идентификаторов Telegram дешевле разбирательств потом.
    op.add_column("runs", sa.Column("questions_message_id", sa.BigInteger(), nullable=True))
    op.add_column("runs", sa.Column("questions_note_id", sa.BigInteger(), nullable=True))
    op.create_check_constraint("ck_runs_questions_of_task", "runs", QUESTIONS_OF_TASK)


def downgrade() -> None:
    # Старый код не знает статуса questions_sent и стадий clarify, answers, approach: такие строки
    # он не уберёт и не продолжит. Удалить или закрыть их руками, осознанно.
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM runs WHERE status IN ('questions_sent', 'clarify', 'answers', 'approach')
          ) THEN
            RAISE EXCEPTION 'runs has rows in P3-11 statuses; close them before downgrade';
          END IF;
        END $$;
        """
    )
    op.drop_constraint("ck_runs_questions_of_task", "runs", type_="check")
    op.drop_column("runs", "questions_note_id")
    op.drop_column("runs", "questions_message_id")
    op.drop_constraint("ck_runs_research_of_task", "runs", type_="check")
    op.drop_column("runs", "research")
