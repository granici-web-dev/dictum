from collections.abc import Iterator

import httpx2
import pytest
from anthropic import DefaultHttpxClient

from app import stages
from app.config import settings
from tests.helpers import InstallResponses


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
