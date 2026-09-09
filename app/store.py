"""Строка прогона в Postgres: кто он, где он и чего ждёт. См. SPEC.md §4 и §4.1.

Работа с базой синхронная: psycopg 3 синхронный, а единственный асинхронный модуль в проекте —
бот, и он зовёт эти функции через `asyncio.to_thread`, тем же приёмом, что и сам обход. Обход о
базе не знает: узнай он о ней, `make run-text` потребовал бы Postgres, чтобы разобрать стадию.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from functools import cache

from pydantic import BaseModel
from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    String,
    create_engine,
    func,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.config import ConfigError, settings
from app.pipeline import NAMES

logger = logging.getLogger(__name__)

AWAITING_CHOICE = "awaiting_choice"
PUBLISHED = "published"
NO_TASK = "no_task"
DROPPED = "dropped"
FAILED = "failed"

# Рабочие статусы — имена стадий: их называет обход, и второй словарь названий разошёлся бы с
# ним. Прогон в рабочем статусе на старте процесса означает, что процесс умер вместе с ним.
WORKING = frozenset(NAMES)
FIRST_STATUS = NAMES[0]


class Base(DeclarativeBase):
    pass


class RunRow(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(8))
    lang: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16))
    auto_approve: Mapped[bool]
    # Заполнены только при `awaiting_choice`: куда возвращаться с ответом человека.
    stopped_stage: Mapped[str | None] = mapped_column(String(16))
    stopped_artifact: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # Остановку ищут по чату и статусу: единственный запрос бота на горячем пути.
        Index("ix_runs_chat_status", "chat_id", "status"),
        # Остановка в чате одна: вторая означала бы, что ответ человека уходит непонятно в
        # какой прогон. Пусть об этом скажет база, а не разбирающийся потом человек.
        Index(
            "ux_runs_one_stop_per_chat",
            "chat_id",
            unique=True,
            postgresql_where=text(f"status = '{AWAITING_CHOICE}'"),
        ),
    )


class Stopped(BaseModel):
    """Прогон, ждущий ответа из чата: всё, чтобы вернуться в него хоть после перезапуска."""

    run_id: str
    lang: str
    auto_approve: bool
    stage: str
    artifact: str


class Orphan(BaseModel):
    """Прогон, переживший свой процесс: человеку о нём скажут, а сам он уже никуда не пойдёт."""

    run_id: str
    chat_id: int
    stage: str


@cache
def engine() -> Engine:
    # Соединение из пула переживает и сон ноутбука, и перезапуск Postgres — но только на бумаге:
    # первый же запрос по протухшему валится, а если это `mark_stage` посреди обхода, прогон
    # умирает из-за икоты базы. Проверка соединения стоит один лишний round-trip.
    return create_engine(settings.database_url, pool_pre_ping=True)


def ensure_schema() -> None:
    """Схема на месте? Забытый `make db` иначе вылезает сырой трассировкой из бота.

    Проверяется рядом с ключами и ffmpeg (SPEC §7.3) и по той же причине: узнавать о настройке
    на первом сообщении со стенда поздно, человек прочтёт это как поломку бота.
    """
    try:
        with engine().connect() as connection:
            connection.execute(select(RunRow.id).limit(1))
    except OperationalError as error:
        raise ConfigError(
            f"База недоступна по {settings.database_url}. Поднимите её: make up."
        ) from error
    except ProgrammingError as error:
        raise ConfigError(
            "В базе нет таблицы runs. Накатите миграции: make db."
        ) from error


@contextmanager
def session() -> Iterator[Session]:
    with Session(engine()) as opened, opened.begin():
        yield opened


def start_run(run_id: str, chat_id: int, source: str, lang: str, auto_approve: bool) -> None:
    with session() as opened:
        opened.add(
            RunRow(
                id=run_id,
                chat_id=chat_id,
                source=source,
                lang=lang,
                status=FIRST_STATUS,
                auto_approve=auto_approve,
            )
        )


def mark_stage(run_id: str, status: str, lang: str) -> None:
    """Прогон закончил стадию `status` и дальше неё не ушёл.

    Не следующую: обход докладывает о законченной, а пойдёт ли он дальше, ещё неизвестно —
    на выборе он тут же встаёт, и строка со стадией, которая не начиналась, врала бы про место
    остановки. Язык пишется тем же ходом: его называет Whisper в ingest, и до записи в строку
    он живёт только в памяти обхода, а владеет им строка (§4.1).
    """
    with session() as opened:
        opened.execute(
            update(RunRow)
            .where(RunRow.id == run_id)
            .values(status=status, lang=lang, stopped_stage=None, stopped_artifact=None)
        )


def stop_on_choice(run_id: str, stage: str, artifact: str) -> None:
    with session() as opened:
        opened.execute(
            update(RunRow)
            .where(RunRow.id == run_id)
            .values(status=AWAITING_CHOICE, stopped_stage=stage, stopped_artifact=artifact)
        )


def finish_run(run_id: str, status: str) -> None:
    with session() as opened:
        opened.execute(
            update(RunRow)
            .where(RunRow.id == run_id)
            .values(status=status, stopped_stage=None, stopped_artifact=None)
        )


def drop_stop(chat_id: int) -> None:
    """Человек ушёл от выбора сам: заговорил голосом или послал `/start` (SPEC §7.3)."""
    with session() as opened:
        opened.execute(
            update(RunRow)
            .where(RunRow.chat_id == chat_id, RunRow.status == AWAITING_CHOICE)
            .values(status=DROPPED, stopped_stage=None, stopped_artifact=None)
        )


def waiting_for(chat_id: int) -> Stopped | None:
    with session() as opened:
        row = opened.scalars(
            select(RunRow).where(RunRow.chat_id == chat_id, RunRow.status == AWAITING_CHOICE)
        ).one_or_none()
        if row is None:
            return None
        if row.stopped_stage is None or row.stopped_artifact is None:
            # Строка из чужих рук: статус ставится только вместе с обоими полями. Отвечать
            # человеку нечем, и пусть его следующее сообщение начнёт новый прогон.
            logger.error("Прогон %s ждёт ответа, но не помнит, где встал", row.id)
            row.status = FAILED
            return None
        return Stopped(
            run_id=row.id,
            lang=row.lang,
            auto_approve=row.auto_approve,
            stage=row.stopped_stage,
            artifact=row.stopped_artifact,
        )


def fail_orphans() -> list[Orphan]:
    """Прогоны, застрявшие в рабочем статусе: их процесс умер, доводить их некому."""
    with session() as opened:
        rows = opened.scalars(select(RunRow).where(RunRow.status.in_(WORKING))).all()
        orphans = [Orphan(run_id=row.id, chat_id=row.chat_id, stage=row.status) for row in rows]
        for row in rows:
            row.status, row.stopped_stage, row.stopped_artifact = FAILED, None, None
        return orphans
