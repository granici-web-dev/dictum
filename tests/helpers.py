import json
import re
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


def thought(text: str, thinking_tokens: int = 900) -> httpx2.Response:
    """Ответ с блоком размышления впереди текста: так отвечает модель, когда думает.

    Текст блока пуст, потому что `display` по умолчанию `omitted`: наружу выходит подпись, а
    счёт размышления приходит отдельным полем `usage`.
    """
    body = message_body(text)
    body["content"] = [{"type": "thinking", "thinking": "", "signature": "sig"}, *body["content"]]
    body["usage"]["output_tokens_details"] = {"thinking_tokens": thinking_tokens}
    return httpx2.Response(200, json=body)


def server_error() -> httpx2.Response:
    return httpx2.Response(
        500,
        headers={"retry-after-ms": "1"},
        json={"type": "error", "error": {"type": "api_error", "message": "overloaded"}},
    )


def heard(text: str, language: str = "russian", duration: float = 12.0) -> httpx2.Response:
    """Ответ Whisper в verbose_json: язык словом по-английски, длительность дробными секундами."""
    return httpx2.Response(
        200,
        json={"task": "transcribe", "text": text, "language": language, "duration": duration},
    )


def request_body(request: httpx2.Request) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(request.content)
    return body


FIXTURES = Path(__file__).parent.parent / "fixtures"
REAL_ISSUES = (FIXTURES / "issues_real.json").read_text(encoding="utf-8")
BROKEN_ISSUES = (FIXTURES / "issues_deferred_without_title.json").read_text(encoding="utf-8")


DEPENDENCY_BELOW = (FIXTURES / "issues_dependency_below.json").read_text(encoding="utf-8")

# Бриф живого прогона 1d03c663aad86f72: продолжение того же выбора, что лежит в
# candidates_multiple.md, — выбрана идея 1, «бот для онбординга новичков».
REAL_BRIEF = (FIXTURES / "brief_real.md").read_text(encoding="utf-8")


def real_issues() -> IssuesFile:
    return IssuesFile.model_validate_json(REAL_ISSUES)


def issues_file(
    directory: Path, text: str = REAL_ISSUES, run_id: str | None = "прогон-ноль"
) -> Path:
    """Кладёт issues.json в каталог прогона. run_id=None — файл, каких decompose больше не пишет."""
    data = json.loads(text)
    if run_id is None:
        data.pop("run_id", None)
    else:
        data["run_id"] = run_id
    path = directory / "issues.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def decompose_answer(issues_json: str = REAL_ISSUES) -> str:
    return f'<file path="outputs/issues.json">\n{issues_json}\n</file>'


def in_board_order(item: dict[str, Any]) -> tuple[float, str]:
    # Позиции равны, когда publish их не задал: тогда порядок решает Trello, а не мы,
    # и двойник не должен изображать, будто порядок вставки что-то гарантирует.
    return item["pos"], item["name"]


class FakeBoard:
    """Доска Trello в памяти: отвечает на запросы клиента и запоминает, что на неё положили."""

    def __init__(self, router: respx.Router) -> None:
        self.board_id = settings.trello_board_id
        self.lists: list[dict[str, Any]] = []
        self.labels: list[dict[str, Any]] = []
        self.cards: list[dict[str, Any]] = []
        self.archived: set[str] = set()
        self.checklists: dict[str, dict[str, Any]] = {}
        self.posts: list[tuple[str, dict[str, str]]] = []
        self.fail_after_cards: int | None = None
        self.busy_replies = 0
        self.counter = 0
        router.route(host="api.trello.com").mock(side_effect=self.handle)

    def new_id(self, kind: str) -> str:
        self.counter += 1
        return f"{kind}-{self.counter}"

    def next_position(self, among: list[dict[str, Any]], asked: str) -> float:
        # Trello сажает "bottom" за последний элемент, "top" перед первым, число берёт как есть.
        # Первый элемент получает 16384, иначе "top" от нуля дал бы ноль и порядок не различался бы.
        if asked not in ("top", "bottom"):
            return float(asked)
        positions: list[float] = [item["pos"] for item in among]
        if not positions:
            return 16384.0
        return max(positions) + 16384 if asked == "bottom" else min(positions) / 2

    def add_list(self, name: str, pos: str = "bottom") -> dict[str, Any]:
        created = {
            "id": self.new_id("list"),
            "name": name,
            "pos": self.next_position(self.lists, pos),
        }
        self.lists.append(created)
        return created

    def list_named(self, name: str) -> dict[str, Any]:
        """Список доски, создаётся при первом обращении: двух списков с одним именем не бывает."""
        here = [item for item in self.lists if item["name"] == name]
        return here[0] if here else self.add_list(name)

    def label_named(self, name: str) -> dict[str, Any]:
        here = [item for item in self.labels if item["name"] == name]
        return here[0] if here else self.add_label(name)

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
        pos: str = "bottom",
    ) -> dict[str, Any]:
        card_id = self.new_id("card")
        here = [card for card in self.cards if card["idList"] == list_id]
        created: dict[str, Any] = {
            "id": card_id,
            "name": name,
            "desc": desc,
            "url": f"https://trello.com/c/{card_id}",
            "idList": list_id,
            "idLabels": [item for item in label_ids.split(",") if item],
            "pos": self.next_position(here, pos),
            "checklists": [],
            "attachments": [],
        }
        self.cards.append(created)
        if archived:
            self.archived.add(card_id)
        return created

    def put_checklist(self, card: dict[str, Any], name: str, items: list[str]) -> dict[str, Any]:
        checklist: dict[str, Any] = {
            "id": self.new_id("checklist"),
            "name": name,
            "checkItems": [{"id": self.new_id("item"), "name": item} for item in items],
        }
        card["checklists"].append(checklist)
        self.checklists[checklist["id"]] = checklist
        return checklist

    def put_attachments(self, card: dict[str, Any], names: list[str]) -> None:
        card["attachments"] += [{"id": self.new_id("attachment"), "name": name} for name in names]

    def card(self, card_id: str) -> dict[str, Any]:
        return next(item for item in self.cards if item["id"] == card_id)

    def card_named(self, name: str) -> dict[str, Any]:
        return next(card for card in self.cards if card["name"] == name)

    def posted(self, path: str) -> list[dict[str, str]]:
        return [fields for posted_path, fields in self.posts if posted_path == path]

    def list_names(self) -> list[str]:
        return [item["name"] for item in sorted(self.lists, key=in_board_order)]

    def card_names(self, list_name: str) -> list[str]:
        list_id = next(item["id"] for item in self.lists if item["name"] == list_name)
        here = [card for card in self.cards if card["idList"] == list_id]
        return [card["name"] for card in sorted(here, key=in_board_order)]

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
            return httpx.Response(200, json=sorted(self.lists, key=in_board_order))
        if path == f"/1/boards/{self.board_id}/labels":
            return httpx.Response(200, json=self.labels)
        if path != f"/1/boards/{self.board_id}/cards":
            raise AssertionError(f"неожиданный GET {path}")
        wanted = request.url.params.get("filter", "open")
        visible = [
            card for card in self.cards if wanted == "all" or card["id"] not in self.archived
        ]
        return httpx.Response(200, json=sorted(visible, key=in_board_order))

    def handle_post(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        fields = dict(parse_qsl(request.content.decode(), keep_blank_values=True))
        self.posts.append((path, fields))
        if path == "/1/lists":
            return httpx.Response(200, json=self.add_list(fields["name"], fields["pos"]))
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
            checklist: dict[str, Any] = {
                "id": self.new_id("checklist"),
                "name": fields["name"],
                "checkItems": [],
            }
            self.card(path.split("/")[3])["checklists"].append(checklist)
            self.checklists[checklist["id"]] = checklist
            return httpx.Response(200, json=checklist)
        if path.endswith("/checkItems"):
            item = {"id": self.new_id("item"), "name": fields["name"]}
            self.checklists[path.split("/")[3]]["checkItems"].append(item)
            return httpx.Response(200, json=item)
        if path.endswith("/attachments"):
            attachment = {"id": self.new_id("attachment"), "name": fields["name"]}
            self.card(path.split("/")[3])["attachments"].append(attachment)
            return httpx.Response(200, json=attachment)
        return httpx.Response(200, json={"id": self.new_id("item"), "name": fields["name"]})


STEPS_DE = (FIXTURES / "steps_de.json").read_text(encoding="utf-8")
CLARIFY_DE = (FIXTURES / "clarify_de.json").read_text(encoding="utf-8")


def steps_answer(change: Callable[[dict[str, Any]], None] = lambda data: None) -> str:
    """Ответ стадии steps по fixtures/steps_de.json: без полей, которые вписывает код.

    Немецкие шаги в фикстуре составлены вручную, под частичный ответ тимлида из
    answers_de_partial.md: живого прогона стадии с ответами ещё не было.
    """
    data: dict[str, Any] = json.loads(STEPS_DE)
    for stamped in ("run_id", "owner_lang", "meeting_lang", "task", "questions"):
        del data[stamped]
    change(data)
    return f'<file path="outputs/steps.json">\n{json.dumps(data, ensure_ascii=False)}\n</file>'


def clarify_answer(change: Callable[[dict[str, Any]], None] = lambda data: None) -> str:
    """Ответ стадии clarify по fixtures/clarify_de.json: без полей, которые вписывает код.

    Немецкие вопросы в фикстуре составлены вручную: живого прогона стадии ещё не было.
    """
    data: dict[str, Any] = json.loads(CLARIFY_DE)
    for stamped in ("run_id", "meeting_lang", "owner_lang", "address_in_text"):
        del data[stamped]
    change(data)
    return f'<file path="outputs/clarify.json">\n{json.dumps(data, ensure_ascii=False)}\n</file>'


WEB_SEARCH_BLOCKS: list[dict[str, Any]] = json.loads(
    (FIXTURES / "web_search_response.json").read_text(encoding="utf-8")
)["content"]
PAUSED_SEARCH_BLOCKS: list[dict[str, Any]] = json.loads(
    (FIXTURES / "web_search_paused.json").read_text(encoding="utf-8")
)["content"]


def searched(text: str, requests: int = 2) -> httpx2.Response:
    """Ответ стадии с поиском: блоки поиска из фикстуры, за ними текст с файлом стадии.

    Текст модели до поиска («поищу…») лежит в фикстуре вне тегов и должен уйти в никуда.
    """
    body = message_body(text)
    body["content"] = [*WEB_SEARCH_BLOCKS, *body["content"]]
    body["usage"]["server_tool_use"] = {"web_search_requests": requests, "web_fetch_requests": 0}
    return httpx2.Response(200, json=body)


def paused_search(requests: int = 1) -> httpx2.Response:
    """Ответ, которым API прервал серверный цикл: последний запрос поиска ещё не отработал."""
    body = message_body("", stop_reason="pause_turn")
    body["content"] = PAUSED_SEARCH_BLOCKS
    body["usage"]["server_tool_use"] = {"web_search_requests": requests, "web_fetch_requests": 0}
    return httpx2.Response(200, json=body)


APPROACH_NEW_DE = (FIXTURES / "approach_new_de.json").read_text(encoding="utf-8")
APPROACH_EXISTING_DE = (FIXTURES / "approach_existing_de.json").read_text(encoding="utf-8")


def approach_answer(
    source: str = APPROACH_NEW_DE, change: Callable[[dict[str, Any]], None] = lambda data: None
) -> str:
    """Ответ стадии approach по фикстуре: без полей, которые вписывает код.

    По умолчанию берётся вывод для нового проекта: снимок стандартов у большинства прогонов в
    тестах пуст, и вывод со ссылками на стандарты там не прошёл бы собственную проверку.
    """
    data: dict[str, Any] = json.loads(source)
    for stamped in ("mode", "stack_named", "searches"):
        del data[stamped]
    del data["recommendation"]["provisional"]
    for reference in data["recommendation"]["standards_refs"]:
        del reference["in_snapshot"]
    for found in data["sources"]:
        del found["found_by_search"]
    change(data)
    return f'<file path="outputs/approach.json">\n{json.dumps(data, ensure_ascii=False)}\n</file>'


MARK = re.compile(r"\[уточнить:[^\]]*\]\s*")


def without_marks(data: dict[str, Any]) -> None:
    """Шаги без пометок неясного: нужны там, где тест убирает вопрос, на который они ссылаются."""
    for step in data["steps"]:
        step["text"] = MARK.sub("", step["text"])
        step["translation"] = MARK.sub("", step["translation"])


def no_questions(data: dict[str, Any]) -> None:
    data["questions"] = []
