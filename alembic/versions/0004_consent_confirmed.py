"""Согласие участников на запись с диктофона: колонка и правило, при каком источнике она есть.

Revision ID: 0004_consent_confirmed
Revises: 0003_brief_dialog
Create Date: 2026-09-17
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0004_consent_confirmed"
down_revision: str | None = "0003_brief_dialog"
branch_labels: None = None
depends_on: None = None

CONSENT_ONLY_FOR_FILE = (
    "(source = 'file' AND consent_confirmed IS TRUE)"
    " OR (source <> 'file' AND consent_confirmed IS NULL)"
)


def upgrade() -> None:
    # NULL, а не NOT NULL DEFAULT false: у текста и голосового чужой записи нет и вопроса о
    # согласии не было, а false читалось бы как «в согласии отказали». false не пишется вовсе:
    # отказ строку не заводит (SPEC §3.1).
    op.add_column("runs", sa.Column("consent_confirmed", sa.Boolean(), nullable=True))
    # Файл без согласия обрабатывать нельзя (§ 201 StGB, CLAUDE.md §8), и пусть это скажет база,
    # а не разбор постфактум. Старые строки — text и voice с NULL — условию уже удовлетворяют.
    op.create_check_constraint("ck_runs_consent_only_for_file", "runs", CONSENT_ONLY_FOR_FILE)


def downgrade() -> None:
    op.drop_constraint("ck_runs_consent_only_for_file", "runs", type_="check")
    op.drop_column("runs", "consent_confirmed")
