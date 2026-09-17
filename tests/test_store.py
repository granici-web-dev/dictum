"""Строка прогона против настоящего Postgres (TESTING.md).

SQLite подменил бы диалект и спрятал ровно то, ради чего таблица заводится: частичный уникальный
индекс и `timestamptz`. Базы нет — тесты пропускаются, а не падают: `make up` не у всех поднят.

Работают тесты в отдельной базе `dictum_test`. Раньше они чистили ту, на которую смотрит
`DATABASE_URL`, а это рабочая база: `make test` после живого прогона стёр его строки, и разбирать
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
from sqlalchemy import delete, select, text
from sqlalchemy.engine import make_url

from app.config import settings
from app.dialog import Turn
from app.pipeline import StopKind
from app.store import (
    AWAITING_ANSWER,
    AWAITING_CHOICE,
    AWAITING_GATE,
    DROPPED,
    FAILED,
    NO_TASK,
    PUBLISHED,
    QUESTIONS_SENT,
    REFUSED,
    REVIEWED,
    STATUS_OF_STOP,
    STOP_STATUSES,
    Base,
    RunRow,
    add_turn,
    child_of,
    drop_stop,
    engine,
    fail_orphans,
    finish_run,
    mark_stage,
    note_questions,
    one_bot_per_database,
    parent_of,
    park_run,
    parked_by_reply,
    reopen_run,
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
INGEST = "ingest"


def address_of_test_database() -> str:
    # render_as_string, а не str(): у SQLAlchemy `str(URL)` прячет пароль звёздочками, и
    # подключение уходит с паролем «***», а отвечает на это сервер отказом в аутентификации.
    return make_url(settings.database_url).set(database=TEST_DATABASE).render_as_string(
        hide_password=False
    )


def created_test_database() -> bool:
    """Заводит `dictum_test` рядом с рабочей базой. False — сервера нет, тестам нечего ждать.

    Подключается к рабочей базе, а не к служебной `postgres`: `CREATE DATABASE` можно послать
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
    working_database_url = settings.database_url
    settings.database_url = address_of_test_database()
    engine.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    yield
    engine().dispose()
    engine.cache_clear()
    settings.database_url = working_database_url


@pytest.fixture
def db(migrated: None) -> Iterator[None]:
    # Проверка не церемония: строку `delete` без `where` отделяет от рабочей базы одна настройка,
    # и однажды она уже смотрела не туда.
    assert settings.database_url.endswith(TEST_DATABASE)
    with session() as opened:
        opened.execute(delete(RunRow))
    yield


def a_stopped_run(run_id: str = "прогон", chat_id: int = 12) -> None:
    start_run(run_id, chat_id, "voice", "ru", True, None, INGEST)
    stop_run(run_id, "choice", "intake", CANDIDATES)


def a_gated_run(run_id: str = "прогон", chat_id: int = 12) -> None:
    start_run(run_id, chat_id, "text", "ru", False, None, INGEST)
    stop_run(run_id, "gate", "brief", BRIEF)


def an_asked_run(run_id: str = "прогон", chat_id: int = 12) -> None:
    start_run(run_id, chat_id, "text", "ru", False, None, INGEST)
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
    start_run("второй", 12, "voice", "ru", True, None, INGEST)

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        stop_run("второй", "choice", "intake", CANDIDATES)


def test_a_gate_stop_collides_with_a_choice_stop_in_the_same_chat(db: None) -> None:
    """Род остановки на счёт не влияет: ответ человека всё равно один, и прогон для него один."""
    a_stopped_run("первый")
    start_run("второй", 12, "text", "ru", False, None, INGEST)

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
    start_run("живой", 12, "voice", "ru", True, None, INGEST)
    mark_stage("живой", "brief", "ru")
    start_run("готовый", 13, "text", "ru", True, None, INGEST)
    finish_run("готовый", PUBLISHED)

    orphans = fail_orphans()

    assert [(orphan.run_id, orphan.chat_id, orphan.stage) for orphan in orphans] == [
        ("живой", 12, "brief")
    ]
    with session() as opened:
        assert opened.get(RunRow, "живой").status == FAILED  # type: ignore[union-attr]
        assert opened.get(RunRow, "готовый").status == PUBLISHED  # type: ignore[union-attr]


def test_fail_orphans_ignores_refused(db: None) -> None:
    """Отвергнутая запись закрыта: уборка на старте не должна объявить её прерванной."""
    start_run("отвергнутый", 12, "file", "ru", False, True, INGEST)
    finish_run("отвергнутый", REFUSED)

    assert fail_orphans() == []
    with session() as opened:
        assert opened.get(RunRow, "отвергнутый").status == REFUSED  # type: ignore[union-attr]


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
    start_run("второй", 12, "text", "ru", False, None, INGEST)

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


def test_start_run_stores_consent_for_file(db: None) -> None:
    start_run("файл", 12, "file", "ru", True, True, INGEST)

    with session() as opened:
        assert opened.get(RunRow, "файл").consent_confirmed is True  # type: ignore[union-attr]


def test_waiting_for_carries_consent(db: None) -> None:
    """Без согласия продолженный файловый прогон был бы тем, что ingest считает незаконным."""
    start_run("файл", 12, "file", "ru", False, True, INGEST)
    stop_run("файл", "gate", "brief", BRIEF)

    stopped = waiting_for(12)

    assert stopped is not None
    assert (stopped.source, stopped.consent_confirmed) == ("file", True)


@pytest.mark.parametrize("consent", [None, False])
def test_check_rejects_file_run_without_consent(db: None, consent: bool | None) -> None:
    """Файл без согласия обрабатывать нельзя (§ 201 StGB), и об этом говорит база, а не код."""
    with pytest.raises(sqlalchemy.exc.IntegrityError, match="ck_runs_consent_only_for_file"):
        start_run("файл", 12, "file", "ru", True, consent, INGEST)


@pytest.mark.parametrize("consent", [True, False])
def test_check_rejects_consent_on_voice_run(db: None, consent: bool) -> None:
    """У голосового чужой записи нет: любое значение врало бы, что вопрос о согласии задавали."""
    with pytest.raises(sqlalchemy.exc.IntegrityError, match="ck_runs_consent_only_for_file"):
        start_run("голос", 12, "voice", "ru", True, consent, INGEST)


def test_a_reviewed_run_is_finished_and_waits_for_nothing(db: None) -> None:
    """Разбор не остановка: он не занимает место остановки в чате и не считается брошенным."""
    start_run("разбор", 12, "file", "de", False, True, INGEST)
    mark_stage("разбор", "review", "de")

    finish_run("разбор", REVIEWED)

    assert REVIEWED not in STOP_STATUSES
    assert waiting_for(12) is None
    assert fail_orphans() == []
    with session() as opened:
        row = opened.get(RunRow, "разбор")

        assert row is not None
        assert (row.status, row.stopped_stage, row.stopped_artifact) == (REVIEWED, None, None)


def a_review(run_id: str = "разбор", chat_id: int = 12, status: str = REVIEWED) -> None:
    start_run(run_id, chat_id, "file", "de", False, True, INGEST)
    finish_run(run_id, status)


def a_task_run(run_id: str, number: int, parent_id: str = "разбор") -> None:
    start_run(run_id, 12, "file", "de", False, None, "assignment", parent_id, number, True)


def an_idea_run(run_id: str, parent_id: str = "разбор") -> None:
    start_run(run_id, 12, "file", "de", False, None, "handoff", parent_id)


def test_a_task_child_and_an_idea_child_are_stored_with_their_parent(db: None) -> None:
    a_review()
    a_task_run("поручение", 1)
    an_idea_run("идея")

    with session() as opened:
        task = opened.get(RunRow, "поручение")
        idea = opened.get(RunRow, "идея")

        assert task is not None and idea is not None
        assert (task.parent_id, task.assignment, task.status) == ("разбор", 1, "assignment")
        assert (idea.parent_id, idea.assignment, idea.status) == ("разбор", None, "handoff")


@pytest.mark.parametrize(("parent_id", "number"), [(None, 1), ("разбор", 0)])
def test_check_rejects_an_assignment_without_a_parent_or_below_one(
    db: None, parent_id: str | None, number: int
) -> None:
    """Номер поручения есть только у ребёнка разбора и считается с единицы, как в кнопке."""
    a_review()

    with pytest.raises(sqlalchemy.exc.IntegrityError, match="ck_runs_assignment_of_parent"):
        start_run(
            "поручение", 12, "text", "de", False, None, "assignment", parent_id, number, True
        )


def test_a_child_of_a_recording_carries_no_consent(db: None) -> None:
    """Согласие дали на запись, и его несёт строка, которая запись получила."""
    a_review()
    a_task_run("без согласия", 1)

    with pytest.raises(sqlalchemy.exc.IntegrityError, match="ck_runs_consent_only_for_file"):
        start_run("с согласием", 12, "file", "de", False, True, "assignment", "разбор", 2, True)


def test_a_file_without_a_parent_still_needs_consent(db: None) -> None:
    with pytest.raises(sqlalchemy.exc.IntegrityError, match="ck_runs_consent_only_for_file"):
        start_run("файл", 12, "file", "de", False, None, INGEST)


def test_second_live_run_for_the_same_task_is_rejected_unless_the_first_was_dropped(
    db: None,
) -> None:
    """Сорванный прогон возобновляют, а брошенный до публикации уступает место новому."""
    a_review()
    a_task_run("первый", 1)
    finish_run("первый", FAILED)

    with pytest.raises(sqlalchemy.exc.IntegrityError, match="ux_runs_one_run_per_task"):
        a_task_run("второй", 1)


def test_a_dropped_task_run_makes_room_for_a_new_one(db: None) -> None:
    a_review()
    a_task_run("первый", 1)
    finish_run("первый", DROPPED)

    a_task_run("второй", 1)

    assert child_of("разбор", 1) is not None


def test_second_live_idea_run_of_one_review_is_rejected_unless_the_first_was_dropped(
    db: None,
) -> None:
    a_review()
    an_idea_run("первая")

    with pytest.raises(sqlalchemy.exc.IntegrityError, match="ux_runs_one_idea_per_review"):
        an_idea_run("вторая")

    finish_run("первая", DROPPED)
    an_idea_run("третья")


def test_a_task_and_the_idea_of_one_review_live_side_by_side(db: None) -> None:
    a_review()
    a_task_run("поручение 1", 1)
    a_task_run("поручение 2", 2)
    an_idea_run("идея")

    children = [child_of("разбор", number) for number in (1, 2, None)]

    assert [child.run_id if child else None for child in children] == [
        "поручение 1",
        "поручение 2",
        "идея",
    ]


@pytest.mark.parametrize("status", [REVIEWED, NO_TASK])
def test_a_finished_review_of_the_same_chat_is_a_parent(db: None, status: str) -> None:
    a_review(status=status)

    parent = parent_of("разбор", 12)

    assert parent is not None
    assert (parent.run_id, parent.lang, parent.source, parent.status) == (
        "разбор",
        "de",
        "file",
        status,
    )


def test_a_review_of_another_chat_is_not_a_parent(db: None) -> None:
    a_review(chat_id=13)

    assert parent_of("разбор", 12) is None


def test_a_run_that_is_not_a_finished_review_is_not_a_parent(db: None) -> None:
    a_review(status=PUBLISHED)

    assert parent_of("разбор", 12) is None


def test_child_of_does_not_see_a_dropped_run(db: None) -> None:
    a_review()
    a_task_run("брошенный", 1)
    finish_run("брошенный", DROPPED)

    assert child_of("разбор", 1) is None


def test_child_of_tells_a_task_from_the_idea(db: None) -> None:
    a_review()
    start_run("поручение", 12, "file", "de", True, None, "assignment", "разбор", 1, True)

    task = child_of("разбор", 1)

    assert task is not None
    assert (task.run_id, task.status, task.auto_approve) == ("поручение", "assignment", True)
    assert child_of("разбор", None) is None
    assert child_of("разбор", 2) is None


def test_a_reopened_run_is_back_at_work_under_the_same_id(db: None) -> None:
    a_review()
    a_task_run("поручение", 1)
    finish_run("поручение", FAILED)

    reopen_run("поручение", "card")

    child = child_of("разбор", 1)
    assert child is not None and (child.run_id, child.status) == ("поручение", "card")


def a_parked_task(run_id: str = "поручение", number: int = 1, chat_id: int = 12) -> None:
    a_task_run(run_id, number)
    park_run(run_id)
    note_questions(run_id, 501, 502)


def test_research_belongs_to_a_task_child_and_only_to_it(db: None) -> None:
    """Выбор ресёрча переживает парковку в строке, и у пути идеи его нет вовсе."""
    a_review()

    with pytest.raises(sqlalchemy.exc.IntegrityError, match="ck_runs_research_of_task"):
        start_run("идея", 12, "file", "de", False, None, "handoff", "разбор", None, True)
    with pytest.raises(sqlalchemy.exc.IntegrityError, match="ck_runs_research_of_task"):
        start_run("поручение", 12, "file", "de", False, None, "assignment", "разбор", 1)


def test_questions_are_sent_only_about_a_task(db: None) -> None:
    a_review()

    with pytest.raises(sqlalchemy.exc.IntegrityError, match="ck_runs_questions_of_task"):
        note_questions("разбор", 501, 502)


def test_parked_run_does_not_block_a_stop_in_the_same_chat(db: None) -> None:
    """Тимлид отвечает днями, и всё это время чат обязан принимать новые встречи и ворота."""
    a_review()
    a_parked_task()
    a_gated_run("ворота")

    stopped = waiting_for(12)

    assert stopped is not None and stopped.run_id == "ворота"


def test_waiting_for_does_not_see_a_parked_run(db: None) -> None:
    a_review()
    a_parked_task()

    assert QUESTIONS_SENT not in STOP_STATUSES
    assert waiting_for(12) is None


def test_fail_orphans_leaves_a_parked_run_alone(db: None) -> None:
    """Припаркованный прогон не держит процесса: перезапуск бота его не обрывает."""
    a_review()
    a_parked_task()

    assert fail_orphans() == []
    assert drop_stop(12) is None
    with session() as opened:
        assert opened.get(RunRow, "поручение").status == QUESTIONS_SENT  # type: ignore[union-attr]


def test_parked_by_reply_finds_the_run_by_either_message(db: None) -> None:
    a_review()
    a_parked_task()

    by_copy, by_note = parked_by_reply(12, 501), parked_by_reply(12, 502)

    assert by_copy is not None and by_copy == by_note
    assert (by_copy.run_id, by_copy.status, by_copy.parent_id, by_copy.assignment) == (
        "поручение",
        QUESTIONS_SENT,
        "разбор",
        1,
    )
    assert parked_by_reply(13, 501) is None
    assert parked_by_reply(12, 503) is None


def test_a_reply_to_questions_already_closed_still_names_the_run(db: None) -> None:
    """Протухший ответ отличает вызывающий: без статуса он завёл бы из ответа новый разбор."""
    a_review()
    a_parked_task()
    mark_stage("поручение", "answers", "de")

    parked = parked_by_reply(12, 502)

    assert parked is not None and parked.status == "answers"


def test_resent_questions_replace_the_messages_a_reply_is_matched_against(db: None) -> None:
    a_review()
    a_parked_task()

    note_questions("поручение", 601, 602)

    assert parked_by_reply(12, 501) is None
    assert parked_by_reply(12, 602) is not None


def test_a_second_run_of_a_parked_task_is_rejected(db: None) -> None:
    """Припаркованный прогон жив: второе нажатие не должно оплатить вопросы второй раз."""
    a_review()
    a_parked_task()

    with pytest.raises(sqlalchemy.exc.IntegrityError, match="ux_runs_one_run_per_task"):
        a_task_run("второй", 1)


def test_child_of_carries_the_research_choice(db: None) -> None:
    a_review()
    start_run("поручение", 12, "file", "de", True, None, "assignment", "разбор", 1, False)

    child = child_of("разбор", 1)

    assert child is not None and child.research is False


def insert_rows(*statements: str) -> None:
    with engine().begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def test_the_upgrade_marks_existing_task_children_as_run_without_research(db: None) -> None:
    """Дети поручения до 0006 шли без ресёрча: миграция записывает факт, а не умолчание."""
    config = Config("alembic.ini")
    command.downgrade(config, "0005_child_runs")
    try:
        insert_rows(
            "insert into runs (id, chat_id, source, lang, status, auto_approve, consent_confirmed)"
            " values ('разбор', 12, 'file', 'de', 'reviewed', false, true)",
            "insert into runs (id, chat_id, source, lang, status, auto_approve, parent_id,"
            " assignment) values ('поручение', 12, 'file', 'de', 'published', false, 'разбор', 1)",
            "insert into runs (id, chat_id, source, lang, status, auto_approve, parent_id)"
            " values ('идея', 12, 'file', 'de', 'published', false, 'разбор')",
        )
    finally:
        command.upgrade(config, "head")

    with session() as opened:
        research = {row.id: row.research for row in opened.scalars(select(RunRow))}

    assert research == {"разбор": None, "поручение": False, "идея": None}


@pytest.mark.parametrize("status", [QUESTIONS_SENT, "clarify", "answers", "approach"])
def test_the_downgrade_refuses_while_rows_sit_in_statuses_the_old_code_does_not_know(
    db: None, status: str
) -> None:
    a_review()
    a_task_run("поручение", 1)
    mark_stage("поручение", status, "de")

    with pytest.raises(sqlalchemy.exc.ProgrammingError, match="P3-11 statuses"):
        command.downgrade(Config("alembic.ini"), "0005_child_runs")

    with engine().connect() as connection:
        context = MigrationContext.configure(connection)
        assert context.get_current_revision() == "0006_teamlead_questions"
