"""Строка прогона против настоящего Postgres (TESTING.md).

SQLite подменил бы диалект и спрятал ровно то, ради чего таблица заводится: частичный уникальный
индекс и `timestamptz`. Базы нет — тесты пропускаются, а не падают: `make up` не у всех поднят.
"""

from collections.abc import Iterator

import pytest
import sqlalchemy
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import delete

from app.config import settings
from app.store import (
    AWAITING_CHOICE,
    FAILED,
    PUBLISHED,
    Base,
    RunRow,
    drop_stop,
    engine,
    fail_orphans,
    finish_run,
    mark_stage,
    session,
    start_run,
    stop_on_choice,
    waiting_for,
)

# Первый прогон миграций дольше двух секунд, которыми ограничены обычные тесты.
pytestmark = [pytest.mark.db, pytest.mark.timeout(30)]

CANDIDATES = "outputs/candidates.md"


def unreachable() -> bool:
    probe = sqlalchemy.create_engine(
        settings.database_url, connect_args={"connect_timeout": 2}
    )
    try:
        with probe.connect():
            return False
    except sqlalchemy.exc.OperationalError:
        return True
    finally:
        probe.dispose()


@pytest.fixture(scope="session")
def migrated() -> Iterator[None]:
    if unreachable():
        pytest.skip(f"Postgres недоступен на {settings.database_url}: сделайте make up")
    command.upgrade(Config("alembic.ini"), "head")
    yield


@pytest.fixture
def db(migrated: None) -> Iterator[None]:
    with session() as opened:
        opened.execute(delete(RunRow))
    yield


def a_stopped_run(run_id: str = "прогон", chat_id: int = 12) -> None:
    start_run(run_id, chat_id, "voice", "ru", True)
    stop_on_choice(run_id, "intake", CANDIDATES)


def test_the_migration_is_what_the_models_say(db: None) -> None:
    """Индекс и типы объявлены дважды: в модели и в миграции, и разойтись им нечем помешать."""
    with engine().connect() as connection:
        context = MigrationContext.configure(connection)

        assert compare_metadata(context, Base.metadata) == []


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
        stop_on_choice("второй", "intake", CANDIDATES)


def test_leaving_the_choice_drops_the_stop_and_keeps_the_run(db: None) -> None:
    a_stopped_run()

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
