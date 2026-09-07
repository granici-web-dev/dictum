import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx2
import pytest
from anthropic import DefaultHttpxClient

from app import stages
from app.config import settings

InstallResponses = Callable[[list[httpx2.Response]], list[httpx2.Request]]


def message_body(text: str, stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 120, "output_tokens": 30},
    }


def ok(text: str, stop_reason: str = "end_turn") -> httpx2.Response:
    return httpx2.Response(200, json=message_body(text, stop_reason))


def server_error() -> httpx2.Response:
    return httpx2.Response(
        500,
        headers={"retry-after-ms": "1"},
        json={"type": "error", "error": {"type": "api_error", "message": "overloaded"}},
    )


def request_body(request: httpx2.Request) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(request.content)
    return body


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> Iterator[InstallResponses]:
    def install(responses: list[httpx2.Response]) -> list[httpx2.Request]:
        requests: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            requests.append(request)
            return responses.pop(0)

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
