"""Двойники внешних границ и отдельная база под тесты, которым нужен настоящий Postgres.

База стоит здесь, а не в tests/test_store.py: строку прогона пишет и бот, и команда с
ноутбука, и проверять её приходится из двух модулей.
"""

from collections.abc import Iterator

import httpx2
import pytest
import respx
import sqlalchemy
from alembic import command
from alembic.config import Config
from anthropic import DefaultHttpxClient
from openai import DefaultHttpxClient as OpenAiHttpxClient
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url

from app import stages, transcribe
from app.config import settings
from app.store import RunRow, engine, session
from tests.helpers import FakeBoard, InstallResponses


# Ключи и флаг живут в .env и попадают в settings при импорте. Тест, который по недосмотру
# доберётся до настоящего клиента, ушёл бы с ними в сеть: так пятнадцать карточек из фикстуры
# однажды уехали на живую доску. Гасим для всех, а llm и board включают себе обратно.
DISARMED = {
    "allow_live_api": False,
    "anthropic_api_key": "",
    "trello_key": "",
    "trello_token": "",
    "trello_board_id": "",
    "trello_idea_board_id": "",
    "telegram_bot_token": "",
    "telegram_allowed_chat_ids": "",
    "openai_api_key": "",
    # Не ключ, но тоже из .env: с KEEP_AUDIO=true у разработчика падал тест на удаление записи.
    "keep_audio": False,
    # В локальном .env может стоять AUTO_APPROVE=true, и тест про ворота падал бы именно там.
    "auto_approve": False,
    # Язык владельца решает, требовать ли перевод: с OWNER_LANG=de в .env немецкий разбор
    # проходил бы без переводов, и тест про пустой перевод падал бы только у того, кто так настроил.
    "owner_lang": "ru",
    # Каталог стандартов рабочего проекта у разработчика в .env не должен попадать ни в снимок
    # теста, ни в запрос к двойнику модели.
    "project_context_dir": "",
    # Папка входящих у разработчика в .env своя, и её отсутствие на другой машине роняло бы
    # старт бота в тестах, которые про папку ничего не знают.
    "meeting_inbox_dir": "",
}


@pytest.fixture(autouse=True)
def disarmed(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in DISARMED.items():
        monkeypatch.setattr(settings, name, value)


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> Iterator[InstallResponses]:
    def install(responses: list[httpx2.Response]) -> list[httpx2.Request]:
        requests: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            requests.append(request)
            return responses.pop(0)

        monkeypatch.setattr(settings, "allow_live_api", True)
        monkeypatch.setattr(settings, "anthropic_api_key", "test")
        monkeypatch.setattr(
            stages,
            "http_client",
            lambda: DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
        )
        stages.anthropic_client.cache_clear()
        return requests

    yield install
    stages.anthropic_client.cache_clear()


@pytest.fixture
def whisper(monkeypatch: pytest.MonkeyPatch) -> InstallResponses:
    """Whisper тем же способом, что и Anthropic: OpenAI SDK тоже на httpx2, мимо respx."""

    def install(responses: list[httpx2.Response]) -> list[httpx2.Request]:
        requests: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            requests.append(request)
            return responses.pop(0)

        monkeypatch.setattr(settings, "allow_live_api", True)
        monkeypatch.setattr(settings, "openai_api_key", "test")
        monkeypatch.setattr(
            transcribe,
            "http_client",
            lambda: OpenAiHttpxClient(transport=httpx2.MockTransport(handler)),
        )
        return requests

    return install


@pytest.fixture
def board(respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch) -> FakeBoard:
    monkeypatch.setattr(settings, "allow_live_api", True)
    monkeypatch.setattr(settings, "trello_key", "test-key")
    monkeypatch.setattr(settings, "trello_token", "test-token")
    monkeypatch.setattr(settings, "trello_board_id", "board1")
    # Двойник держит одну доску: путь идеи и путь поручения пишут на неё оба, а тест, которому
    # важно, куда именно, разводит доски сам.
    monkeypatch.setattr(settings, "trello_idea_board_id", "board1")
    monkeypatch.setattr(settings, "project_key", "DCT")
    return FakeBoard(respx_mock)


TEST_DATABASE = "dictum_test"


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
