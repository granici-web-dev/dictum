import logging
from pathlib import Path
from typing import cast

import pytest
from telegram import Message
from telegram.error import TelegramError

from app import deliver
from app.deliver import send_tasks_and_documents
from app.pipeline import REVIEW_MD, TRANSCRIPT
from app.render import review_messages
from app.review import Review
from tests.test_review import REVIEW_DE, REVIEW_NONE

RUN = "прогон-разбора"


class ChatOfDelivery:
    """Чат, который помнит, что и в каком порядке ушло. `failing` — текст, который не примут."""

    def __init__(self, failing: str | None = None) -> None:
        self.sent: list[str | Path] = []
        self.keyboards: list[object] = []
        self.failing = failing

    async def reply_text(self, text: str, reply_markup: object = None) -> Message:
        if self.failing is not None and text.startswith(self.failing):
            raise TelegramError("сообщение не ушло")
        self.sent.append(text)
        self.keyboards.append(reply_markup)
        return cast(Message, self)

    async def reply_document(self, document: Path) -> Message:
        self.sent.append(document)
        return cast(Message, self)


@pytest.fixture(autouse=True)
def without_pauses(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """Шов паузы: тест считает её вызовы, а настоящая секунда сна тут ничего не проверяет."""
    counted: list[None] = []

    async def at_once() -> None:
        counted.append(None)

    monkeypatch.setattr(deliver, "wait_between_task_messages", at_once)
    return counted


def review_of(review_json: str) -> Review:
    return Review.model_validate_json(review_json)


def parts_of(review_json: str) -> list[str]:
    return [part for parts in review_messages(review_of(review_json)) for part in parts]


@pytest.mark.asyncio
async def test_delivery_counts_messages_that_did_not_reach_the_chat(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """На трёх поручениях потерянное было терпимо, на двадцати это потеря работы."""
    review = review_of(REVIEW_DE)
    lost = review_messages(review)[1][-1]
    chat = ChatOfDelivery(failing=lost[:40])

    with caplog.at_level(logging.INFO, logger="app.deliver"):
        delivery = await send_tasks_and_documents(cast(Message, chat), RUN, tmp_path, review)

    assert delivery.failed == 1
    assert delivery.sent == len(parts_of(REVIEW_DE)) - 1 + 2
    assert lost not in chat.sent
    assert f"run={RUN} delivered={delivery.sent} failed=1" in caplog.text


@pytest.mark.asyncio
async def test_delivery_paces_assignment_messages(
    tmp_path: Path, without_pauses: list[None]
) -> None:
    """Telegram держит примерно сообщение в секунду: двадцать подряд поймали бы 429."""
    review = review_of(REVIEW_DE)

    await send_tasks_and_documents(cast(Message, ChatOfDelivery()), RUN, tmp_path, review)

    assert len(without_pauses) == len(review.tasks) - 1


@pytest.mark.asyncio
async def test_delivery_sends_both_documents_on_an_empty_review(tmp_path: Path) -> None:
    """«Заданий нет» проверяют по расшифровке, и уходит она даже при пустом разборе."""
    chat = ChatOfDelivery()

    delivery = await send_tasks_and_documents(
        cast(Message, chat), RUN, tmp_path, review_of(REVIEW_NONE)
    )

    assert chat.sent == [tmp_path / REVIEW_MD, tmp_path / TRANSCRIPT]
    assert (delivery.sent, delivery.failed) == (2, 0)
