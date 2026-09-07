"""Транспорт к Trello REST: авторизация, эндпоинты, повтор на 429 и 5xx. См. SPEC.md §6.

Отображение issues на карточки живёт в app/publish.py, здесь его нет.
"""

import time
from typing import Any

import httpx
from pydantic import BaseModel, Field

from app.config import LiveApiNotAllowed, MissingApiKey, settings

API = "https://api.trello.com/1"
TOO_MANY_REQUESTS = 429
SERVER_ERRORS = frozenset({500, 502, 503, 504})
ATTEMPTS = 3
RETRY_PAUSE_SECONDS = 1.0


class TrelloList(BaseModel):
    id: str
    name: str


class TrelloLabel(BaseModel):
    id: str
    name: str


class TrelloChecklist(BaseModel):
    id: str
    name: str


class TrelloAttachment(BaseModel):
    id: str
    name: str


class TrelloCard(BaseModel):
    id: str
    name: str
    desc: str
    url: str
    list_id: str = Field(alias="idList")
    label_ids: list[str] = Field(alias="idLabels", default_factory=list)
    checklists: list[TrelloChecklist] = Field(default_factory=list)
    attachments: list[TrelloAttachment] = Field(default_factory=list)


def pause_before_retry(response: httpx.Response, attempt: int) -> float:
    asked: str = response.headers.get("retry-after", "")
    if asked.isdigit():
        return float(asked)
    doubling: int = 2**attempt
    return RETRY_PAUSE_SECONDS * doubling


def worth_retrying(method: str, status: int) -> bool:
    # Ошибку сервера повторяем только на чтении: POST мог дойти до Trello и потерять ответ,
    # и второй такой же создал бы вторую карточку. Дубль на доске хуже упавшего прогона.
    return status == TOO_MANY_REQUESTS or (method == "GET" and status in SERVER_ERRORS)


class Trello:
    def __init__(self, client: httpx.Client, key: str, token: str, board_id: str) -> None:
        self.client = client
        self.auth = {"key": key, "token": token}
        self.board_id = board_id

    def close(self) -> None:
        self.client.close()

    def send(
        self,
        method: str,
        path: str,
        query: dict[str, str] | None = None,
        fields: dict[str, str] | None = None,
    ) -> Any:
        attempt = 0
        while True:
            response = self.client.request(
                method,
                f"{API}{path}",
                params={**self.auth, **(query or {})},
                data=fields,
            )
            if attempt == ATTEMPTS - 1 or not worth_retrying(method, response.status_code):
                response.raise_for_status()
                return response.json()
            time.sleep(pause_before_retry(response, attempt))
            attempt += 1

    def get(self, path: str, **query: str) -> Any:
        return self.send("GET", path, query=query)

    def post(self, path: str, **fields: str) -> Any:
        return self.send("POST", path, fields=fields)

    def lists(self) -> list[TrelloList]:
        return [
            TrelloList.model_validate(item) for item in self.get(f"/boards/{self.board_id}/lists")
        ]

    def create_list(self, name: str) -> TrelloList:
        return TrelloList.model_validate(self.post("/lists", name=name, idBoard=self.board_id))

    def labels(self) -> list[TrelloLabel]:
        return [
            TrelloLabel.model_validate(item) for item in self.get(f"/boards/{self.board_id}/labels")
        ]

    def create_label(self, name: str, color: str) -> TrelloLabel:
        return TrelloLabel.model_validate(
            self.post("/labels", name=name, color=color, idBoard=self.board_id)
        )

    def cards(self) -> list[TrelloCard]:
        # Чеклисты и вложения приезжают тем же запросом: publish должен уметь дособрать карточку,
        # созданную оборвавшимся прогоном, а отдельный GET на каждую упёрся бы в лимит запросов.
        # filter=all включает архивные: карточка, убранная с доски, всё равно существует,
        # и создавать её вторую publish не должен.
        return [
            TrelloCard.model_validate(item)
            for item in self.get(
                f"/boards/{self.board_id}/cards",
                fields="name,desc,url,idList,idLabels",
                filter="all",
                checklists="all",
                checklist_fields="name",
                attachments="true",
                attachment_fields="name",
            )
        ]

    def create_card(
        self, list_id: str, name: str, description: str, label_ids: list[str]
    ) -> TrelloCard:
        return TrelloCard.model_validate(
            self.post(
                "/cards",
                idList=list_id,
                name=name,
                desc=description,
                idLabels=",".join(label_ids),
            )
        )

    def add_checklist(self, card_id: str, name: str, items: list[str]) -> TrelloChecklist:
        checklist = TrelloChecklist.model_validate(
            self.post(f"/cards/{card_id}/checklists", name=name)
        )
        for item in items:
            self.post(f"/checklists/{checklist.id}/checkItems", name=item)
        return checklist

    def attach_url(self, card_id: str, url: str, name: str) -> None:
        self.post(f"/cards/{card_id}/attachments", url=url, name=name)


def open_trello() -> Trello:
    if not settings.allow_live_api:
        raise LiveApiNotAllowed(
            "ALLOW_LIVE_API is not true, nothing was sent. "
            "Set ALLOW_LIVE_API=true in .env for a publish you mean to make."
        )
    missing = [
        name
        for name, value in (
            ("TRELLO_KEY", settings.trello_key),
            ("TRELLO_TOKEN", settings.trello_token),
            ("TRELLO_BOARD_ID", settings.trello_board_id),
        )
        if not value
    ]
    if missing:
        raise MissingApiKey(
            f"{', '.join(missing)} is not set. Copy .env.example to .env and fill it in."
        )
    return Trello(
        httpx.Client(timeout=30),
        settings.trello_key,
        settings.trello_token,
        settings.trello_board_id,
    )
