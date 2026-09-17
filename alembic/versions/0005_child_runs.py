"""Дочерний прогон разбора: ссылка на родителя, номер поручения и один живой прогон на выбор.

Revision ID: 0005_child_runs
Revises: 0004_consent_confirmed
Create Date: 2026-09-17
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0005_child_runs"
down_revision: str | None = "0004_consent_confirmed"
branch_labels: None = None
depends_on: None = None

ASSIGNMENT_OF_PARENT = "assignment IS NULL OR (parent_id IS NOT NULL AND assignment >= 1)"
CONSENT_BEFORE = (
    "(source = 'file' AND consent_confirmed IS TRUE)"
    " OR (source <> 'file' AND consent_confirmed IS NULL)"
)
CONSENT_NOW = (
    "(source = 'file' AND parent_id IS NULL AND consent_confirmed IS TRUE)"
    " OR ((source <> 'file' OR parent_id IS NOT NULL) AND consent_confirmed IS NULL)"
)
ONE_RUN_PER_TASK = "assignment IS NOT NULL AND status <> 'dropped'"
ONE_IDEA_PER_REVIEW = "parent_id IS NOT NULL AND assignment IS NULL AND status <> 'dropped'"


def upgrade() -> None:
    op.add_column("runs", sa.Column("parent_id", sa.String(32), nullable=True))
    op.create_foreign_key("fk_runs_parent_id_runs", "runs", "runs", ["parent_id"], ["id"])
    # Номер поручения в разборе родителя, с единицы, как в кнопке. NULL у ребёнка значит «вся
    # запись как идея», у прогона без родителя он NULL всегда.
    op.add_column("runs", sa.Column("assignment", sa.SmallInteger(), nullable=True))
    op.create_check_constraint("ck_runs_assignment_of_parent", "runs", ASSIGNMENT_OF_PARENT)
    # Ребёнок записи не трогает: согласие остаётся фактом одной строки, родительской. source
    # ребёнок наследует, поэтому файл с parent_id согласия не несёт.
    op.drop_constraint("ck_runs_consent_only_for_file", "runs", type_="check")
    op.create_check_constraint("ck_runs_consent_only_for_file", "runs", CONSENT_NOW)
    # Один живой прогон на выбор из разбора: поручение N или вся запись как идея. dropped не
    # считается: «Стоп» и /start снимают прогон до публикации. failed считается: сорванный прогон
    # возобновляется тем же run_id, иначе частичная публикация получила бы вторые карточки под
    # новым маркером. Два индекса, а не один по coalesce(assignment, 0): индекс по выражению
    # alembic не сверяет, и compare_metadata перестал бы их видеть.
    op.create_index(
        "ux_runs_one_run_per_task",
        "runs",
        ["parent_id", "assignment"],
        unique=True,
        postgresql_where=sa.text(ONE_RUN_PER_TASK),
    )
    op.create_index(
        "ux_runs_one_idea_per_review",
        "runs",
        ["parent_id"],
        unique=True,
        postgresql_where=sa.text(ONE_IDEA_PER_REVIEW),
    )


def downgrade() -> None:
    # Отказ, если дочерние строки есть: старый CHECK им не удовлетворяет, а удалять их молча значит
    # терять связь карточек доски с разбором. Удалить руками, осознанно.
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM runs WHERE parent_id IS NOT NULL) THEN
            RAISE EXCEPTION
              'runs has child runs (parent_id IS NOT NULL); delete them before downgrade';
          END IF;
        END $$;
        """
    )
    op.drop_index("ux_runs_one_idea_per_review", table_name="runs")
    op.drop_index("ux_runs_one_run_per_task", table_name="runs")
    op.drop_constraint("ck_runs_consent_only_for_file", "runs", type_="check")
    op.create_check_constraint("ck_runs_consent_only_for_file", "runs", CONSENT_BEFORE)
    op.drop_constraint("ck_runs_assignment_of_parent", "runs", type_="check")
    op.drop_constraint("fk_runs_parent_id_runs", "runs", type_="foreignkey")
    op.drop_column("runs", "assignment")
    op.drop_column("runs", "parent_id")
