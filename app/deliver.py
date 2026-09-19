"""Разбор встречи в чат: поручение сообщением с кнопками, затем файлы. См. SPEC.md §7.3.

Общий для бота и для `make meeting`: разбор доставляется одинаково, откуда бы прогон ни шёл.
"""

import asyncio
import logging
from collections.abc import Awaitable
from pathlib import Path

from pydantic import BaseModel
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.error import TelegramError

from app.pipeline import REVIEW_MD, TRANSCRIPT
from app.render import review_messages
from app.review import Review

logger = logging.getLogger(__name__)

IDEA = "idea"

IDEA_BUTTON = "Проработать как идею"

TASK_BUTTON = "Разложить на шаги"

# Вторая кнопка под тем же поручением: решение о ресёрче принимается по задаче и на глазах у неё,
# а не режимом чата, который забылся бы включённым.
BARE = "bare"

TASK_BARE_BUTTON = "Шаги без ресёрча"

# Telegram держит около одного сообщения в секунду на чат, и двадцать поручений подряд поймали бы
# 429. Двадцать секунд пауз на фоне пятиминутной расшифровки не стоят ничего.
PAUSE_BETWEEN_TASK_MESSAGES = 1.0


class Delivery(BaseModel):
    """Сколько сообщений разбора дошло до чата и сколько нет.

    Потерянное поручение — потерянная работа: кнопки под ним больше нет, и о двадцати сообщениях
    человек сам не заметит, какого не хватает. Поэтому не ушедшие считаются и называются вслух.
    """

    sent: int
    failed: int


def idea_keyboard(run_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(IDEA_BUTTON, callback_data=f"{IDEA}:{run_id}")]]
    )


def task_keyboard(run_id: str, number: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(TASK_BUTTON, callback_data=f"task:{run_id}:{number}"),
                InlineKeyboardButton(
                    TASK_BARE_BUTTON, callback_data=f"task:{run_id}:{number}:{BARE}"
                ),
            ]
        ]
    )


async def wait_between_task_messages() -> None:
    await asyncio.sleep(PAUSE_BETWEEN_TASK_MESSAGES)


async def reached_the_chat(sending: Awaitable[Message]) -> bool:
    """Ушло ли сообщение. Сорванное не отнимает у человека остальные и расшифровку под ними."""
    try:
        await sending
    except TelegramError:
        return False
    return True


async def send_tasks_and_documents(
    reply_to: Message, run_id: str, root: Path, review: Review
) -> Delivery:
    """Поручения сообщениями в порядке встречи, затем разбор и расшифровка файлами.

    Склеивать поручения нельзя: кнопка «Разложить на шаги» несёт номер и обязана стоять под своим
    текстом. Расшифровка уходит и при пустом разборе: «заданий нет» проверяют как раз по ней.
    """
    reached: list[bool] = []
    for number, parts in enumerate(review_messages(review), start=1):
        if number > 1:
            await wait_between_task_messages()
        for text in parts[:-1]:
            reached.append(await reached_the_chat(reply_to.reply_text(text)))
        # Кнопка на последней части: под ней поручение уже прочитано целиком.
        reached.append(
            await reached_the_chat(
                reply_to.reply_text(parts[-1], reply_markup=task_keyboard(run_id, number))
            )
        )
    for document in (REVIEW_MD, TRANSCRIPT):
        reached.append(await reached_the_chat(reply_to.reply_document(root / document)))
    delivery = Delivery(sent=reached.count(True), failed=reached.count(False))
    logger.info(
        "run=%s delivered=%d failed=%d", run_id, delivery.sent, delivery.failed
    )
    return delivery
