"""Строка прогона против настоящего Postgres (TESTING.md).

SQLite подменил бы диалект и спрятал ровно то, ради чего таблица заводится: частичный уникальный
индекс и `timestamptz`. Базы нет — тесты пропускаются, а не падают: `make up` не у всех поднят.

Работают тесты в отдельной базе `dictum_test`. Раньше они чистили ту, на которую смотрит
`DATABASE_URL`, а это база стенда: `make test` после живого прогона стёр его строки, и разбирать
пропажу пришлось глазами.
"""

from collections.abc import Iterator
from typing import get_args

import pytest
import sqlalchemy
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url

from app.config import settings
from app.dialog import Turn
from app.pipeline import StopKind
from app.store import (
    AWAITING_ANSWER,
    AWAITING_CHOICE,
    AWAITING_GATE,
    FAILED,
    PUBLISHED,
    STATUS_OF_STOP,
    Base,
    RunRow,
    add_turn,
    drop_stop,
    engine,
    fail_orphans,
    finish_run,
    mark_stage,
    one_bot_per_database,
    session,
    start_run,
    stop_run,
    waiting_for,
)

# Первый прогон миграций дольше двух секунд, которыми ограничены обычные тесты.
pytestmark = [pytest.mark.db, pytest.mark.timeout(30)]

CANDIDATES = "outputs/candidates.md"
BRIEF = "outputs/brief.md"
QUESTION = "outputs/brief_question.md"
TEST_DATABASE = "dictum_test"


def address_of_test_database() -> str:
    # render_as_string, а не str(): у SQLAlchemy `str(URL)` прячет пароль звёздочками, и
    # подключение уходит с паролем «***», а отвечает на это сервер отказом в аутентификации.
    return make_url(settings.database_url).set(database=TEST_DATABASE).render_as_string(
        hide_password=False
    )


def created_test_database() -> bool:
    """Заводит `dictum_test` рядом со стендовой базой. False — сервера нет, тестам нечего ждать.

    Подключается к стендовой базе, а не к служебной `postgres`: `CREATE DATABASE` можно послать
    из любой, а на этой машине служебная отвечает отказом в аутентификации.
    """
    server = sqlalchemy.create_engine(
        settings.database_url,
        connect_args={"connect_timeout": 2},
        isolation_level="AUTOCOMMIT",
    )
    try:
        with server.connect() as connection:
            known = connection.scalar(
                text("select 1 from pg_database where datname = :name"),
                {"name": TEST_DATABASE},
            )
            if not known:
                connection.execute(text(f'create database "{TEST_DATABASE}"'))
        return True
    except sqlalchemy.exc.OperationalError:
        return False
    finally:
        server.dispose()


@pytest.fixture(scope="session")
def migrated() -> Iterator[None]:
    if not created_test_database():
        pytest.skip(f"Postgres недоступен на {settings.database_url}: сделайте make up")
    stand = settings.database_url
    settings.database_url = address_of_test_database()
    engine.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    yield
    engine().dispose()
    engine.cache_clear()
    settings.database_url = stand


@pytest.fixture
def db(migrated: None) -> Iterator[None]:
    # Проверка не церемония: строку `delete` без `where` отделяет от базы стенда одна настройка,
    # и однажды она уже смотрела не туда.
    assert settings.database_url.endswith(TEST_DATABASE)
    with session() as opened:
        opened.execute(delete(RunRow))
    yield


def a_stopped_run(run_id: str = "прогон", chat_id: int = 12) -> None:
    start_run(run_id, chat_id, "voice", "ru", True)
    stop_run(run_id, "choice", "intake", CANDIDATES)


def a_gated_run(run_id: str = "прогон", chat_id: int = 12) -> None:
    start_run(run_id, chat_id, "text", "ru", False)
    stop_run(run_id, "gate", "brief", BRIEF)


def an_asked_run(run_id: str = "прогон", chat_id: int = 12) -> None:
    start_run(run_id, chat_id, "text", "ru", False)
    stop_run(run_id, "answer", "brief", QUESTION)


def test_the_migration_is_what_the_models_say(db: None) -> None:
    """Колонки, их типы, длины и nullability в миграции и в модели совпадают.

    Чего эта сверка не видит: предиката частичного индекса и server default — `compare_metadata`
    их не сравнивает (проверено подменой предиката на выдуманный, расхождений ноль). За тем, что
    индекс уникален на обоих статусах остановки, следят тесты про две остановки в одном чате.
    """
    with engine().connect() as connection:
        context = MigrationContext.configure(connection)

        assert compare_metadata(context, Base.metadata) == []


def test_a_second_bot_does_not_get_the_database(db: None) -> None:
    """Второй бот метит живой прогон первого сорванным и пишет об этом человеку."""
    with one_bot_per_database() as first, one_bot_per_database() as second:
        assert first
        assert not second


def test_the_lock_goes_away_with_the_bot_that_held_it(db: None) -> None:
    """Убитый бот не оставляет базу занятой: блокировка живёт не дольше соединения."""
    with one_bot_per_database() as first:
        assert first

    with one_bot_per_database() as next_one:
        assert next_one


def test_a_stop_is_found_by_the_chat_it_waits_on(db: None) -> None:
    a_stopped_run()

    stopped = waiting_for(12)

    assert stopped is not None
    assert (stopped.run_id, stopped.stage, stopped.artifact) == ("прогон", "intake", CANDIDATES)
    assert stopped.lang == "ru"


def test_a_new_process_finds_the_stop_the_old_one_left(db: None) -> None:
    """Ради этого P2-02 и делался: перезапуск бота больше не теряет остановку."""
    a_stopped_run()
    engine().dispose()
    engine.cache_clear()

    stopped = waiting_for(12)

    assert stopped is not None and stopped.run_id == "прогон"


def test_the_answer_moves_the_run_on_and_the_stop_is_gone(db: None) -> None:
    a_stopped_run()

    mark_stage("прогон", "brief", "ru")

    assert waiting_for(12) is None


def test_a_chat_cannot_hold_two_stops_at_once(db: None) -> None:
    """Вторая остановка означала бы, что ответ человека уходит непонятно в какой прогон."""
    a_stopped_run("первый")
    start_run("второй", 12, "voice", "ru", True)

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        stop_run("второй", "choice", "intake", CANDIDATES)


def test_a_gate_stop_collides_with_a_choice_stop_in_the_same_chat(db: None) -> None:
    """Род остановки на счёт не влияет: ответ человека всё равно один, и прогон для него один."""
    a_stopped_run("первый")
    start_run("второй", 12, "text", "ru", False)

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        stop_run("второй", "gate", "brief", BRIEF)


def test_leaving_the_choice_drops_the_stop_and_keeps_the_run(db: None) -> None:
    a_stopped_run()

    assert drop_stop(12) == "прогон"

    assert waiting_for(12) is None


def test_dropping_a_stop_in_a_chat_that_has_none_names_no_run(db: None) -> None:
    """Бот пишет это в лог: «остановку сняли» и «остановки не было» — два разных события."""
    assert drop_stop(12) is None


def test_a_gate_stop_is_found_with_its_kind_and_the_source_of_the_run(db: None) -> None:
    """Род остановки несёт статус, а source — подпись прогресса у продолженного прогона."""
    a_gated_run()

    stopped = waiting_for(12)

    assert stopped is not None
    assert (stopped.kind, stopped.stage, stopped.artifact) == ("gate", "brief", BRIEF)
    assert (stopped.source, stopped.auto_approve) == ("text", False)


def test_leaving_a_gate_drops_the_stop_too(db: None) -> None:
    """«Стоп» на воротах, голосовое и `/start` — один и тот же выход из остановки."""
    a_gated_run()

    drop_stop(12)

    assert waiting_for(12) is None


def test_orphans_are_named_and_closed_but_finished_runs_are_left_alone(db: None) -> None:
    start_run("живой", 12, "voice", "ru", True)
    mark_stage("живой", "brief", "ru")
    start_run("готовый", 13, "text", "ru", True)
    finish_run("готовый", PUBLISHED)

    orphans = fail_orphans()

    assert [(orphan.run_id, orphan.chat_id, orphan.stage) for orphan in orphans] == [
        ("живой", 12, "brief")
    ]
    with session() as opened:
        assert opened.get(RunRow, "живой").status == FAILED  # type: ignore[union-attr]
        assert opened.get(RunRow, "готовый").status == PUBLISHED  # type: ignore[union-attr]


def test_moving_on_forgets_where_the_run_had_stopped(db: None) -> None:
    """Иначе строка в рабочем статусе носит место старой остановки, и §4 про неё врёт."""
    a_stopped_run()

    mark_stage("прогон", "brief", "ru")

    with session() as opened:
        row = opened.get(RunRow, "прогон")

        assert row is not None
        assert (row.stopped_stage, row.stopped_artifact) == (None, None)


def test_a_stopped_run_keeps_where_it_stopped(db: None) -> None:
    a_stopped_run()

    with session() as opened:
        row = opened.get(RunRow, "прогон")

        assert row is not None
        assert (row.status, row.stopped_stage, row.stopped_artifact) == (
            AWAITING_CHOICE,
            "intake",
            CANDIDATES,
        )


def test_a_gated_run_waits_under_its_own_status(db: None) -> None:
    a_gated_run()

    with session() as opened:
        row = opened.get(RunRow, "прогон")

        assert row is not None
        assert (row.status, row.stopped_stage, row.stopped_artifact) == (
            AWAITING_GATE,
            "brief",
            BRIEF,
        )


def test_every_kind_of_stop_has_a_status_of_its_own(db: None) -> None:
    """Род остановки несёт статус, и забытый род оставил бы прогон без места возврата."""
    assert set(STATUS_OF_STOP) == set(get_args(StopKind))
    assert len(set(STATUS_OF_STOP.values())) == len(STATUS_OF_STOP)


def test_a_run_waiting_for_an_answer_waits_under_its_own_status(db: None) -> None:
    an_asked_run()

    stopped = waiting_for(12)

    assert stopped is not None
    assert (stopped.kind, stopped.stage, stopped.artifact) == ("answer", "brief", QUESTION)
    with session() as opened:
        assert opened.get(RunRow, "прогон").status == AWAITING_ANSWER  # type: ignore[union-attr]


def test_a_question_stop_collides_with_a_gate_stop_in_the_same_chat(db: None) -> None:
    """Третий род остановки считается тем же индексом: ответ человека уходит в один прогон."""
    an_asked_run("первый")
    start_run("второй", 12, "text", "ru", False)

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        stop_run("второй", "gate", "brief", BRIEF)


def test_the_dialog_comes_back_with_the_stop_in_the_order_it_was_said(db: None) -> None:
    """Ответы человека не лежат ни в одном артефакте: без строки стадия спросит их заново."""
    an_asked_run()
    add_turn("прогон", "Кто пользователь?\n", "Наша же команда")
    add_turn("прогон", "Как часто?\n", "Каждый день")

    stopped = waiting_for(12)

    assert stopped is not None
    assert stopped.turns == (
        Turn(question="Кто пользователь?\n", answer="Наша же команда"),
        Turn(question="Как часто?\n", answer="Каждый день"),
    )


def test_a_run_that_was_never_asked_anything_carries_an_empty_dialog(db: None) -> None:
    a_gated_run()

    stopped = waiting_for(12)

    assert stopped is not None and stopped.turns == ()
