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
    CheckConstraint,
    DateTime,
    Index,
    String,
    create_engine,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.config import ConfigError, settings
from app.dialog import Turn
from app.ingest import Source
from app.pipeline import NAMES, StopKind

logger = logging.getLogger(__name__)

# Номер произвольный, но постоянный: по нему бот узнаёт, что база уже занята другим ботом.
BOT_LOCK = 20260902

AWAITING_CHOICE = "awaiting_choice"
AWAITING_GATE = "awaiting_gate"
AWAITING_ANSWER = "awaiting_answer"
PUBLISHED = "published"
# Разбор встречи отдан человеку, и прогон на этом закончен: это не остановка, путь до карточек
# по поручению заводит уже другой прогон (P3-08).
REVIEWED = "reviewed"
NO_TASK = "no_task"
# Запись отвергнута до расшифровки: её никто не слушал. `no_task` сказал бы о её содержимом, что
# задания в ней нет, а этого никто не знает.
REFUSED = "refused"
DROPPED = "dropped"
FAILED = "failed"

# Рабочие статусы — имена стадий: их называет обход, и второй словарь названий разошёлся бы с
# ним. Прогон в рабочем статусе на старте процесса означает, что процесс умер вместе с ним.
WORKING = frozenset(NAMES)
FIRST_STATUS = NAMES[0]

# Род остановки статус и несёт: все три ждут ответа из одного чата и различаются только тем,
# чего от человека ждут. Отдельной колонки под род поэтому нет.
STATUS_OF_STOP: dict[StopKind, str] = {
    "choice": AWAITING_CHOICE,
    "gate": AWAITING_GATE,
    "answer": AWAITING_ANSWER,
}
KIND_OF_STOP: dict[str, StopKind] = {status: kind for kind, status in STATUS_OF_STOP.items()}
STOP_STATUSES = frozenset(STATUS_OF_STOP.values())


class Base(DeclarativeBase):
    pass


class RunRow(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    source: Mapped[Source] = mapped_column(String(8))
    lang: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16))
    auto_approve: Mapped[bool]
    # Заполнены только у остановленного прогона: куда возвращаться с ответом человека.
    stopped_stage: Mapped[str | None] = mapped_column(String(16))
    stopped_artifact: Mapped[str | None] = mapped_column(String(64))
    # Ходы brief-диалога (SPEC §3.3): вопрос стадии и ответ человека на него. Ответы не лежат
    # ни в одном артефакте, а пережить перезапуск обязаны — потому и живут в строке.
    brief_dialog: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    # Есть только у файла с диктофона и только согласием (SPEC §4): у текста и голосового чужой
    # записи нет, и вопроса о согласии им не задавали.
    consent_confirmed: Mapped[bool | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "(source = 'file' AND consent_confirmed IS TRUE)"
            " OR (source <> 'file' AND consent_confirmed IS NULL)",
            name="ck_runs_consent_only_for_file",
        ),
        # Остановку ищут по чату и статусу: единственный запрос бота на горячем пути.
        Index("ix_runs_chat_status", "chat_id", "status"),
        # Остановка в чате одна, какого бы рода она ни была: вторая означала бы, что ответ
        # человека уходит непонятно в какой прогон. Пусть об этом скажет база, а не
        # разбирающийся потом человек.
        Index(
            "ux_runs_one_stop_per_chat",
            "chat_id",
            unique=True,
            postgresql_where=text(
                "status in ("
                + ", ".join(f"'{status}'" for status in sorted(STATUS_OF_STOP.values()))
                + ")"
            ),
        ),
    )


class Stopped(BaseModel):
    """Прогон, ждущий ответа из чата: всё, чтобы вернуться в него хоть после перезапуска."""

    run_id: str
    lang: str
    source: Source
    consent_confirmed: bool | None
    auto_approve: bool
    kind: StopKind
    stage: str
    artifact: str
    # Пусто у всех остановок, кроме вопроса брифа, и у первого его вопроса тоже: отвечать не на
    # что было.
    turns: tuple[Turn, ...] = ()


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
    на первом сообщении поздно, человек прочтёт это как поломку бота.
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


@contextmanager
def one_bot_per_database() -> Iterator[bool]:
    """Держит блокировку базы, пока бот жив. False — база уже занята другим ботом.

    Второй бот на ту же базу не просто дублирует прогоны: его уборка на старте (`fail_orphans`)
    метит живой прогон первого сорванным и пишет об этом человеку, пока тот прогон спокойно
    доходит до карточек. Снимаем блокировку явно: соединение уходит в пул живым, и Postgres
    держал бы её за уже вышедшим ботом. Убитый процесс роняет соединение, и база снимает её сама.
    """
    with engine().connect() as connection:
        held = bool(connection.scalar(select(func.pg_try_advisory_lock(BOT_LOCK))))
        try:
            yield held
        finally:
            if held:
                connection.scalar(select(func.pg_advisory_unlock(BOT_LOCK)))


def start_run(
    run_id: str,
    chat_id: int,
    source: Source,
    lang: str,
    auto_approve: bool,
    consent_confirmed: bool | None,
) -> None:
    with session() as opened:
        opened.add(
            RunRow(
                id=run_id,
                chat_id=chat_id,
                source=source,
                lang=lang,
                status=FIRST_STATUS,
                auto_approve=auto_approve,
                consent_confirmed=consent_confirmed,
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


def stop_run(run_id: str, kind: StopKind, stage: str, artifact: str) -> None:
    with session() as opened:
        opened.execute(
            update(RunRow)
            .where(RunRow.id == run_id)
            .values(
                status=STATUS_OF_STOP[kind], stopped_stage=stage, stopped_artifact=artifact
            )
        )


def finish_run(run_id: str, status: str) -> None:
    with session() as opened:
        opened.execute(
            update(RunRow)
            .where(RunRow.id == run_id)
            .values(status=status, stopped_stage=None, stopped_artifact=None)
        )


def drop_stop(chat_id: int) -> str | None:
    """Человек ушёл от остановки сам: заговорил голосом, послал `/start` или нажал «Стоп».

    Отдаёт брошенный прогон, чтобы бот назвал его в логе: без имени запись «остановку сняли»
    не отличить от «остановки не было», а в чате это два разных события.
    """
    with session() as opened:
        return opened.scalars(
            update(RunRow)
            .where(RunRow.chat_id == chat_id, RunRow.status.in_(STOP_STATUSES))
            .values(status=DROPPED, stopped_stage=None, stopped_artifact=None)
            .returning(RunRow.id)
        ).one_or_none()


def waiting_for(chat_id: int) -> Stopped | None:
    with session() as opened:
        row = opened.scalars(
            select(RunRow).where(RunRow.chat_id == chat_id, RunRow.status.in_(STOP_STATUSES))
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
            source=row.source,
            consent_confirmed=row.consent_confirmed,
            auto_approve=row.auto_approve,
            kind=KIND_OF_STOP[row.status],
            stage=row.stopped_stage,
            artifact=row.stopped_artifact,
            turns=tuple(Turn.model_validate(turn) for turn in row.brief_dialog),
        )


def add_turn(run_id: str, question: str, answer: str) -> None:
    """Записывает ход brief-диалога: этот вопрос стадии и этот ответ человека (SPEC §3.3).

    Пишется целым списком, а не дописыванием на месте: SQLAlchemy заметит только присвоение,
    и правка внутри JSON уехала бы в никуда молча.
    """
    with session() as opened:
        row = opened.scalars(select(RunRow).where(RunRow.id == run_id)).one()
        row.brief_dialog = [*row.brief_dialog, {"question": question, "answer": answer}]


def fail_orphans() -> list[Orphan]:
    """Прогоны, застрявшие в рабочем статусе: их процесс умер, доводить их некому."""
    with session() as opened:
        rows = opened.scalars(select(RunRow).where(RunRow.status.in_(WORKING))).all()
        orphans = [Orphan(run_id=row.id, chat_id=row.chat_id, stage=row.status) for row in rows]
        for row in rows:
            row.status, row.stopped_stage, row.stopped_artifact = FAILED, None, None
        return orphans
