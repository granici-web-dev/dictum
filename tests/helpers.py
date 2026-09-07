import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2


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


REAL_ISSUES = (Path(__file__).parent.parent / "fixtures/issues_real.json").read_text(
    encoding="utf-8"
)


def decompose_answer(issues_json: str = REAL_ISSUES) -> str:
    return (
        f'<file path="outputs/issues.json">\n{issues_json}\n</file>\n'
        '<file path="outputs/issues.md">\n# Issues\n\n## Фаза 1\n</file>'
    )
