"""Telegram-бот демо-пути: текст → весь пайплайн → карточки в Trello. См. SPEC.md §7.3.

Единственный асинхронный модуль (CONVENTIONS): пайплайн синхронный и уходит в поток, а обратно
докладывает через цикл событий. Решения, которые можно принять без Telegram, вынесены функциями —
обвес обработчиков тестировать незачем.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import suppress
from datetime import timedelta
from pathlib import Path

from telegram import Message, Update, Voice
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from app.config import ConfigError, LiveApiNotAllowed, MissingApiKey, settings
from app.ingest import Source, new_run_id
from app.pipeline import Stage, stages_between
from app.run import Run, walk
from app.transcribe import FFMPEG_MISSING, TranscriptionError, ffmpeg_installed

# Имя задано строкой, а не __name__: модуль запускают как `python -m`, и там __name__ — это
# "__main__", мимо дерева "app", которому в конце файла поднимают уровень до INFO. С __name__
# такие записи до человека не доходят.
logger = logging.getLogger("app.bot")

RUNS = Path("runs")
FIRST_STAGE = "ingest"
LAST_STAGE = "publish"
JOURNAL = "outputs/publish.json"
VOICE_FILE = "inputs/voice.oga"

# Ограничение стенда, а не Whisper: минута записи стоит копейки, а вот стадии за ней думают тем
# дольше, чем длиннее идея, и очередь у стенда этого не прощает. Нарезка длинного — P3-03.
# В минутах, потому что в минутах об этом говорят человеку: с секундами отказ однажды сказал бы
# «длиннее 1 минут» на лимите в 90 с.
MAX_VOICE_MINUTES = 2
MAX_VOICE_SECONDS = MAX_VOICE_MINUTES * 60

LABEL = {
    "ingest": "принял идею",
    "intake": "выделил суть",
    "brief": "собрал бриф",
    "research": "ресёрч пропущен",
    "prd": "написал PRD",
    "decompose": "разбил на задачи",
    "publish": "опубликовал в Trello",
}

# Голосовое расшифровывается на той же стадии, что принимает текст, а метка стадии живёт в
# сообщении и с «▸», и с «✓»: отглагольное существительное читается верно в обоих.
VOICE_INGEST_LABEL = "расшифровка голосового"

GREETING = (
    "Пришлите идею голосовым или текстом, одну за раз. Я доведу её до карточек в Trello и дам "
    "ссылку.\nЗаймёт около трёх минут, о каждом шаге буду писать здесь же."
)

BUSY = "Прогон уже идёт, дождитесь его конца."

TOO_LONG = (
    f"Голосовое длиннее {MAX_VOICE_MINUTES} минут я пока не расшифровываю. "
    "Наговорите покороче или пришлите текстом."
)

UNSUPPORTED = (
    "Принимаю голосовое и текст. Файл с диктофона будет позже: к нему нужно подтверждение, "
    "что все участники записи согласны."
)

VOICE_NOT_TAKEN = "Не смог забрать голосовое из Telegram. Пришлите его ещё раз."

# Кандидатов intake отдаёт и когда идей несколько, и когда не нашёл ни одной, поэтому отказ их
# не считает: на двухсекундном голосовом бот сообщал «в сообщении несколько идей», а в
# артефакте стояло «идея не найдена».
NO_SINGLE_IDEA = (
    "Не смог выделить одну идею. Пришлите её одним сообщением и чуть подробнее: "
    "что нужно сделать и для кого."
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


def progress_text(run_id: str, done: list[str], source: Source) -> str:
    labels = dict(LABEL)
    if source == "voice":
        labels[FIRST_STAGE] = VOICE_INGEST_LABEL
    lines = [f"Прогон {run_id}", ""]
    marked_current = False
    for stage in stages_between(FIRST_STAGE, LAST_STAGE):
        if stage.name in done:
            mark = "✓"
        elif not marked_current:
            mark, marked_current = "▸", True
        else:
            mark = "·"
        lines.append(f"{mark} {labels[stage.name]}")
    return "\n".join(lines)


def board_url() -> str:
    return f"https://trello.com/b/{settings.trello_board_id}"


def cards_published(root: Path) -> int:
    return len(json.loads((root / JOURNAL).read_text(encoding="utf-8")))


def finished_text(root: Path) -> str:
    return f"Готово: {cards_published(root)} карточек.\n{board_url()}"


def source_of(run: Run) -> Source:
    return "voice" if run.audio else "text"


def demo_run(run_id: str, text: str = "", audio: Path | None = None) -> Run:
    """Прогон стенда. Ворота сняты флагом (SPEC §3.2), а не тем, что кнопок ещё нет."""
    return Run(
        root=RUNS / run_id,
        run_id=run_id,
        lang=settings.default_lang,
        text=text,
        audio=audio,
        auto_approve=True,
    )


def permitted(message: Message) -> bool:
    if message.chat_id in allowed_chats():
        return True
    logger.warning("refusal=stranger chat=%s: чата нет в списке разрешённых", message.chat_id)
    return False


async def refuse(message: Message, tag: str, text: str) -> None:
    """Отвечает отказом и оставляет счётную запись.

    Отчёт репетиции (P2-06) отвечает на вопрос «сколько человек упёрлось» числом, а не памятью,
    поэтому у каждого отказа свой tag: `grep -c "refusal=busy"` и есть ответ.
    """
    logger.info("refusal=%s chat=%s", tag, message.chat_id)
    await message.reply_text(text)


def voice_seconds(voice: Voice) -> int:
    # Сегодня PTB отдаёт число при любом входе, и ветка с timedelta недостижима. Она стоит
    # потому, что тип объявлен `int | timedelta`, а с флагом PTB_TIMEDELTA (который станет
    # умолчанием) станет достижимой. Тест на неё написать нечем: флаг читается при импорте.
    duration = voice.duration
    return round(duration.total_seconds()) if isinstance(duration, timedelta) else duration


def too_long(voice: Voice) -> bool:
    return voice_seconds(voice) > MAX_VOICE_SECONDS


async def save_voice(voice: Voice, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    await (await voice.get_file()).download_to_drive(target)


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(GREETING)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not message.text or not permitted(message):
        return
    if running.locked():
        await refuse(message, "busy", BUSY)
        return

    async with running:
        run = demo_run(new_run_id(), text=message.text)
        note = await message.reply_text(progress_text(run.run_id, [], source_of(run)))
        await follow(note, run)


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or message.voice is None or not permitted(message):
        return
    if too_long(message.voice):
        # Длительность в записи, а не только тег: на репетиции важно, насколько именно
        # переговорили лимит, иначе непонятно, двигать его или оставить.
        logger.info(
            "refusal=too_long chat=%s seconds=%d",
            message.chat_id,
            voice_seconds(message.voice),
        )
        await message.reply_text(TOO_LONG)
        return
    if running.locked():
        await refuse(message, "busy", BUSY)
        return

    async with running:
        run_id = new_run_id()
        audio = RUNS / run_id / VOICE_FILE
        run = demo_run(run_id, audio=audio)
        # Сообщение о ходе — до скачивания: на конференционном wi-fi голосовое едет секунды,
        # и всё это время человек не должен смотреть в пустой чат.
        note = await message.reply_text(progress_text(run.run_id, [], source_of(run)))
        try:
            await save_voice(message.voice, audio)
        except TelegramError:
            logger.exception("Прогон %s не забрал голосовое", run_id)
            await note.edit_text(VOICE_NOT_TAKEN)
            return
        await follow(note, run)


async def on_anything_else(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not permitted(message):
        return
    await refuse(message, "unsupported", UNSUPPORTED)


async def follow(note: Message, run: Run) -> None:
    """Гонит прогон в потоке и правит одно сообщение до самого конца."""
    done: list[str] = []
    loop = asyncio.get_running_loop()
    progress: list[Future[Message | bool]] = []

    def report(stage: Stage) -> None:
        # Обход идёт в рабочем потоке, а правка сообщения живёт в цикле событий.
        done.append(stage.name)
        progress.append(
            asyncio.run_coroutine_threadsafe(
                note.edit_text(progress_text(run.run_id, done, source_of(run))), loop
            )
        )

    ending = await outcome(run, report)
    # Правки прогресса ответа Telegram не ждут, поэтому финальная обязана уйти после них: на
    # прогоне 0ac7bdffe0e0ba58 ответ на последнюю правку пришёл вторым и затёр ссылку списком
    # галочек. Прогон выглядел законченным, в логе было чисто, а результата человек не увидел.
    # Сбой правки прогресса финалу не помеха: она косметическая, ссылка — нет.
    for edit in progress:
        with suppress(TelegramError):
            await asyncio.wrap_future(edit)
    await note.edit_text(ending)


async def outcome(run: Run, report: Callable[[Stage], None]) -> str:
    """Чем кончился прогон, одной строкой человеку."""
    try:
        waiting = await asyncio.to_thread(walk, run, FIRST_STAGE, LAST_STAGE, report)
    except TranscriptionError as error:
        # Текст такой ошибки написан человеку, а не в лог: показываем как есть.
        logger.warning("Прогон %s не расшифровал запись: %s", run.run_id, error)
        return str(error)
    except Exception:
        # Единственная точка перехвата на прогон: одно сообщение человеку, одна запись в лог.
        logger.exception("Прогон %s не дошёл до конца", run.run_id)
        return f"Прогон {run.run_id} сорвался. Подробности в логе, попробуйте ещё раз."

    if waiting and waiting.kind == "choice":
        logger.info("stop=choice run=%s", run.run_id)
        return NO_SINGLE_IDEA
    if waiting:
        logger.info("stop=gate run=%s stage=%s", run.run_id, waiting.stage)
        return (
            f"Прогон {run.run_id} встал на воротах после стадии {waiting.stage}: "
            "подтвердить их в чате пока нечем."
        )
    return finished_text(run.root)


def main() -> None:
    if not settings.telegram_bot_token:
        raise MissingApiKey("TELEGRAM_BOT_TOKEN не задан. Скопируйте .env.example в .env.")
    if not settings.allow_live_api:
        raise LiveApiNotAllowed(
            "ALLOW_LIVE_API is not true. Бот существует, чтобы тратить ключ, и без флага "
            "он отвечал бы отказом на каждое сообщение."
        )
    allowed_chats()
    # Ключ и ffmpeg — на старте по одной причине: без любого из них голосовое не расшифровать, а
    # узнать об этом на первом сообщении со стенда значит показать людям «прогон сорвался».
    if not settings.openai_api_key:
        raise MissingApiKey(
            "OPENAI_API_KEY не задан, а без него голосовое не расшифровать. "
            "Скопируйте .env.example в .env и заполните."
        )
    if not ffmpeg_installed():
        raise ConfigError(FFMPEG_MISSING)

    # Без concurrent_updates бот разбирает обновления по одному и второе сообщение достаёт из
    # очереди только после того, как вернётся обработчик первого, то есть через весь прогон.
    # Отказ «прогон уже идёт» при этом недостижим, а человек три минуты не получает ничего и
    # потом оплачивает свой прогон. Однопрогонность стережёт замок, а не очередь апдейтов.
    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(True)
        .build()
    )
    application.add_handler(CommandHandler("start", on_start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    application.add_handler(MessageHandler(filters.VOICE, on_voice))
    # Последним и почти без фильтра по типу: молчание в ответ на присланный файл или на опечатку
    # в команде человек у стенда читает как поломку бота. /start сюда не доходит, его забирает
    # обработчик выше. Служебные события чата (кто-то вошёл, сменилось название) под отказ не
    # попадают — им никто ничего не присылал.
    application.add_handler(MessageHandler(~filters.StatusUpdate.ALL, on_anything_else))
    application.run_polling()


if __name__ == "__main__":
    logging.basicConfig(format="%(levelname)s %(message)s")
    logging.getLogger("app").setLevel(logging.INFO)
    main()
