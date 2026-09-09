from collections.abc import Iterator

import httpx2
import pytest
import respx
from anthropic import DefaultHttpxClient
from openai import DefaultHttpxClient as OpenAiHttpxClient

from app import stages, transcribe
from app.config import settings
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
    "telegram_bot_token": "",
    "telegram_allowed_chat_ids": "",
    "openai_api_key": "",
    # Не ключ, но тоже из .env: с KEEP_AUDIO=true у разработчика падал тест на удаление записи.
    "keep_audio": False,
    # На стенде в .env стоит AUTO_APPROVE=true, и тест про ворота падал бы именно там.
    "auto_approve": False,
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
def whisper(monkeypatch: pytest.MonkeyPatch) -> Iterator[InstallResponses]:
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
        transcribe.whisper_client.cache_clear()
        return requests

    yield install
    transcribe.whisper_client.cache_clear()


@pytest.fixture
def board(respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch) -> FakeBoard:
    monkeypatch.setattr(settings, "allow_live_api", True)
    monkeypatch.setattr(settings, "trello_key", "test-key")
    monkeypatch.setattr(settings, "trello_token", "test-token")
    monkeypatch.setattr(settings, "trello_board_id", "board1")
    monkeypatch.setattr(settings, "project_key", "DCT")
    return FakeBoard(respx_mock)
