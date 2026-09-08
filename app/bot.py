"""Telegram-бот демо-пути: текст → весь пайплайн → карточки в Trello. См. SPEC.md §7.3.

Единственный асинхронный модуль (CONVENTIONS): пайплайн синхронный и уходит в поток, а обратно
докладывает через цикл событий. Решения, которые можно принять без Telegram, вынесены функциями —
обвес обработчиков тестировать незачем.
"""

import asyncio
import json
import logging
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from app.config import ConfigError, LiveApiNotAllowed, MissingApiKey, settings
from app.ingest import new_run_id
from app.pipeline import Stage, stages_between
from app.run import Run, walk

# Имя задано строкой, а не __name__: модуль запускают как `python -m`, и там __name__ — это
# "__main__", мимо дерева "app", которому в конце файла поднимают уровень до INFO. С __name__
# такие записи до человека не доходят.
logger = logging.getLogger("app.bot")

RUNS = Path("runs")
FIRST_STAGE = "ingest"
LAST_STAGE = "publish"
JOURNAL = "outputs/publish.json"

LABEL = {
    "ingest": "принял идею",
    "intake": "выделил суть",
    "brief": "собрал бриф",
    "research": "ресёрч пропущен",
    "prd": "написал PRD",
    "decompose": "разбил на задачи",
    "publish": "опубликовал в Trello",
}

GREETING = (
    "Пришлите идею текстом, одну за раз. Я доведу её до карточек в Trello и дам ссылку.\n"
    "Займёт около трёх минут, о каждом шаге буду писать здесь же."
)

running = asyncio.Lock()


def allowed_chats() -> frozenset[int]:
    written = settings.telegram_allowed_chat_ids.split(",")
    listed = [part.strip() for part in written if part.strip()]
    if not listed:
        raise ConfigError(
            "TELEGRAM_ALLOWED_CHAT_IDS пуст, бот не запущен. Имя бота публично, и каждое "
            "сообщение тратит ключ, поэтому список обязателен. Свой id покажет @userinfobot."
        )
    unreadable = [part for part in listed if not part.lstrip("-").isdigit()]
    if unreadable:
        raise ConfigError(
            f"TELEGRAM_ALLOWED_CHAT_IDS: {', '.join(unreadable)} — это не id чата. "
            "Нужны целые числа через запятую, у групп они отрицательные."
        )
    return frozenset(int(part) for part in listed)


def progress_text(run_id: str, done: list[str]) -> str:
    lines = [f"Прогон {run_id}", ""]
    marked_current = False
    for stage in stages_between(FIRST_STAGE, LAST_STAGE):
        if stage.name in done:
            mark = "✓"
        elif not marked_current:
            mark, marked_current = "▸", True
        else:
            mark = "·"
        lines.append(f"{mark} {LABEL[stage.name]}")
    return "\n".join(lines)


def board_url() -> str:
    return f"https://trello.com/b/{settings.trello_board_id}"


def cards_published(root: Path) -> int:
    return len(json.loads((root / JOURNAL).read_text(encoding="utf-8")))


def finished_text(root: Path) -> str:
    return f"Готово: {cards_published(root)} карточек.\n{board_url()}"


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(GREETING)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not message.text:
        return
    if message.chat_id not in allowed_chats():
        logger.warning("Сообщение из чата %s, которого нет в списке разрешённых", message.chat_id)
        return
    if running.locked():
        await message.reply_text("Прогон уже идёт, дождитесь его конца.")
        return

    async with running:
        run_id = new_run_id()
        run = Run(
            root=RUNS / run_id,
            run_id=run_id,
            lang=settings.default_lang,
            text=message.text,
        )
        done: list[str] = []
        note = await message.reply_text(progress_text(run_id, done))
        loop = asyncio.get_running_loop()

        def report(stage: Stage) -> None:
            # Обход идёт в рабочем потоке, а правка сообщения живёт в цикле событий.
            done.append(stage.name)
            asyncio.run_coroutine_threadsafe(note.edit_text(progress_text(run_id, done)), loop)

        try:
            waiting = await asyncio.to_thread(walk, run, FIRST_STAGE, LAST_STAGE, report)
        except Exception:
            # Единственная точка перехвата на прогон: одно сообщение человеку, одна запись в лог.
            logger.exception("Прогон %s не дошёл до конца", run_id)
            await note.edit_text(
                f"Прогон {run_id} сорвался. Подробности в логе, попробуйте ещё раз."
            )
            return

        if waiting:
            await note.edit_text("В сообщении несколько идей. Пришлите одну.")
            return
        await note.edit_text(finished_text(run.root))


def main() -> None:
    if not settings.telegram_bot_token:
        raise MissingApiKey("TELEGRAM_BOT_TOKEN не задан. Скопируйте .env.example в .env.")
    if not settings.allow_live_api:
        raise LiveApiNotAllowed(
            "ALLOW_LIVE_API is not true. Бот существует, чтобы тратить ключ, и без флага "
            "он отвечал бы отказом на каждое сообщение."
        )
    allowed_chats()

    application = Application.builder().token(settings.telegram_bot_token).build()
    application.add_handler(CommandHandler("start", on_start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    application.run_polling()


if __name__ == "__main__":
    logging.basicConfig(format="%(levelname)s %(message)s")
    logging.getLogger("app").setLevel(logging.INFO)
    main()
