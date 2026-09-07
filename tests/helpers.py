import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import httpx2
import respx

from app.config import settings
from app.models import IssuesFile


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


FIXTURES = Path(__file__).parent.parent / "fixtures"
REAL_ISSUES = (FIXTURES / "issues_real.json").read_text(encoding="utf-8")
BROKEN_ISSUES = (FIXTURES / "issues_title_too_long.json").read_text(encoding="utf-8")


def real_issues() -> IssuesFile:
    return IssuesFile.model_validate_json(REAL_ISSUES)


def decompose_answer(issues_json: str = REAL_ISSUES) -> str:
    return (
        f'<file path="outputs/issues.json">\n{issues_json}\n</file>\n'
        '<file path="outputs/issues.md">\n# Issues\n\n## Фаза 1\n</file>'
    )


class FakeBoard:
    """Доска Trello в памяти: отвечает на запросы клиента и запоминает, что на неё положили."""

    def __init__(self, router: respx.Router) -> None:
        self.board_id = settings.trello_board_id
        self.lists: list[dict[str, Any]] = []
        self.labels: list[dict[str, Any]] = []
        self.cards: list[dict[str, Any]] = []
        self.archived: set[str] = set()
        self.posts: list[tuple[str, dict[str, str]]] = []
        self.fail_after_cards: int | None = None
        self.busy_replies = 0
        self.counter = 0
        router.route(host="api.trello.com").mock(side_effect=self.handle)

    def new_id(self, kind: str) -> str:
        self.counter += 1
        return f"{kind}-{self.counter}"

    def add_list(self, name: str) -> dict[str, Any]:
        created = {"id": self.new_id("list"), "name": name}
        self.lists.append(created)
        return created

    def add_label(self, name: str) -> dict[str, Any]:
        created = {"id": self.new_id("label"), "name": name}
        self.labels.append(created)
        return created

    def add_card(
        self,
        name: str,
        desc: str,
        list_id: str = "list-0",
        label_ids: str = "",
        archived: bool = False,
        pos: str = "1",
    ) -> dict[str, Any]:
        card_id = self.new_id("card")
        created: dict[str, Any] = {
            "id": card_id,
            "name": name,
            "desc": desc,
            "url": f"https://trello.com/c/{card_id}",
            "idList": list_id,
            "idLabels": [item for item in label_ids.split(",") if item],
            "pos": float(pos),
            "checklists": [],
            "attachments": [],
        }
        self.cards.append(created)
        if archived:
            self.archived.add(card_id)
        return created

    def card(self, card_id: str) -> dict[str, Any]:
        return next(item for item in self.cards if item["id"] == card_id)

    def card_named(self, name: str) -> dict[str, Any]:
        return next(card for card in self.cards if card["name"] == name)

    def posted(self, path: str) -> list[dict[str, str]]:
        return [fields for posted_path, fields in self.posts if posted_path == path]

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.busy_replies > 0:
            self.busy_replies -= 1
            return httpx.Response(429, headers={"retry-after": "0"}, json={"message": "rate limit"})
        if request.method == "GET":
            return self.handle_get(request)
        return self.handle_post(request)

    def handle_get(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/1/boards/{self.board_id}/lists":
            return httpx.Response(200, json=self.lists)
        if path == f"/1/boards/{self.board_id}/labels":
            return httpx.Response(200, json=self.labels)
        if path != f"/1/boards/{self.board_id}/cards":
            raise AssertionError(f"неожиданный GET {path}")
        wanted = request.url.params.get("filter", "open")
        visible = [
            card for card in self.cards if wanted == "all" or card["id"] not in self.archived
        ]
        return httpx.Response(200, json=visible)

    def handle_post(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        fields = dict(parse_qsl(request.content.decode()))
        self.posts.append((path, fields))
        if path == "/1/lists":
            return httpx.Response(200, json=self.add_list(fields["name"]))
        if path == "/1/labels":
            return httpx.Response(200, json=self.add_label(fields["name"]))
        if path == "/1/cards":
            if self.fail_after_cards is not None and len(self.cards) >= self.fail_after_cards:
                return httpx.Response(500, json={"message": "Trello is down"})
            return httpx.Response(
                200,
                json=self.add_card(
                    fields["name"],
                    fields["desc"],
                    fields["idList"],
                    fields["idLabels"],
                    pos=fields["pos"],
                ),
            )
        if path.endswith("/checklists"):
            checklist = {"id": self.new_id("checklist"), "name": fields["name"]}
            self.card(path.split("/")[3])["checklists"].append(checklist)
            return httpx.Response(200, json=checklist)
        if path.endswith("/attachments"):
            attachment = {"id": self.new_id("attachment"), "name": fields["name"]}
            self.card(path.split("/")[3])["attachments"].append(attachment)
            return httpx.Response(200, json=attachment)
        return httpx.Response(200, json={"id": self.new_id("item"), "name": fields["name"]})
