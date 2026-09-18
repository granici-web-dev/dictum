"""Прогон помнит все свои сообщения с вопросами: reply на любую копию продолжает его.

Revision ID: 0007_questions_message_ids
Revises: 0006_teamlead_questions
Create Date: 2026-09-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_questions_message_ids"
down_revision: str | None = "0006_teamlead_questions"
branch_labels: None = None
depends_on: None = None

QUESTIONS_OF_TASK = "cardinality(questions_message_ids) = 0 OR assignment IS NOT NULL"
PAIR_OF_QUESTIONS = (
    "(questions_message_id IS NULL AND questions_note_id IS NULL) OR assignment IS NOT NULL"
)


def upgrade() -> None:
    # Пара колонок держала только последнюю отправку, и reply на первую копию вопросов уходил
    # новым разбором, теряя ответ тимлида (живая проверка части D P3-11).
    op.add_column(
        "runs",
        sa.Column(
            "questions_message_ids",
            postgresql.ARRAY(sa.BigInteger()),
            nullable=False,
            server_default=sa.text("'{}'::bigint[]"),
        ),
    )
    # Порядок тот же, в каком сообщения уходили в чат: сначала текст для копирования, потом
    # перевод с кнопками. array_remove с NULL выбрасывает пустую половину пары.
    op.execute(
        """
        UPDATE runs
           SET questions_message_ids =
               array_remove(ARRAY[questions_message_id, questions_note_id], NULL)
         WHERE questions_message_id IS NOT NULL OR questions_note_id IS NOT NULL
        """
    )
    op.drop_constraint("ck_runs_questions_of_task", "runs", type_="check")
    op.drop_column("runs", "questions_note_id")
    op.drop_column("runs", "questions_message_id")
    op.create_check_constraint("ck_runs_questions_of_task", "runs", QUESTIONS_OF_TASK)


def downgrade() -> None:
    # Третье и дальше сообщения в пару колонок не влезают. Обрезать их молча значит вернуть тот
    # самый дефект, из-за которого массив и появился: reply на потерянную копию не узнается.
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM runs WHERE cardinality(questions_message_ids) > 2) THEN
            RAISE EXCEPTION 'runs has rows with more than two questions messages; '
                            'close those runs before downgrade';
          END IF;
        END $$;
        """
    )
    op.add_column("runs", sa.Column("questions_message_id", sa.BigInteger(), nullable=True))
    op.add_column("runs", sa.Column("questions_note_id", sa.BigInteger(), nullable=True))
    op.execute(
        """
        UPDATE runs
           SET questions_message_id = questions_message_ids[1],
               questions_note_id = questions_message_ids[2]
         WHERE cardinality(questions_message_ids) > 0
        """
    )
    op.drop_constraint("ck_runs_questions_of_task", "runs", type_="check")
    op.drop_column("runs", "questions_message_ids")
    op.create_check_constraint("ck_runs_questions_of_task", "runs", PAIR_OF_QUESTIONS)
