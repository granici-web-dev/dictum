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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from telegram import Message, Update, Voice
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from app.candidates import Candidates, parse_candidates
from app.config import ConfigError, LiveApiNotAllowed, MissingApiKey, settings
from app.ingest import new_run_id
from app.pipeline import ISSUES_JSON, NAMES, Stage, stages_between
from app.publish import journal_of
from app.run import Pause, Redo, Run, read_artifact, walk
from app.store import (
    AWAITING_CHOICE,
    FAILED,
    NO_TASK,
    ensure_schema,
    PUBLISHED,
    Stopped,
    drop_stop,
    fail_orphans,
    finish_run,
    mark_stage,
    start_run,
    stop_on_choice,
    waiting_for,
)
from app.transcribe import FFMPEG_MISSING, NothingHeard, TranscriptionError, ffmpeg_installed

# Имя задано строкой, а не __name__: модуль запускают как `python -m`, и там __name__ — это
# "__main__", мимо дерева "app", которому в конце файла поднимают уровень до INFO. С __name__
# такие записи до человека не доходят.
logger = logging.getLogger("app.bot")

RUNS = Path("runs")
FIRST_STAGE = "ingest"
LAST_STAGE = "publish"
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
    "ссылку.\nЭто займёт несколько минут — я буду писать после каждого шага."
)

BUSY = "Прогон уже идёт, дождитесь его конца."

EMPTY = "Пустое сообщение. Пришлите идею словами или номер из списка."

TOO_LONG = (
    f"Голосовое длиннее {MAX_VOICE_MINUTES} минут я пока не расшифровываю. "
    "Наговорите покороче или пришлите текстом."
)

UNSUPPORTED = (
    "Принимаю голосовое и текст. Файл с диктофона будет позже: к нему нужно подтверждение, "
    "что все участники записи согласны."
)

VOICE_NOT_TAKEN = "Не смог забрать голосовое из Telegram. Пришлите его ещё раз."

HEARD = "Вот что я услышал:"

PICK_ONE = "Пришлите номер или саму идею словами — я продолжу этот же прогон."

NOTHING_HEARD = "Задания в записи я не нашёл."

DISCUSSED = "Вот о чём в ней говорили:"

ASK_AGAIN = "Пришлите идею одним сообщением и чуть подробнее: что нужно сделать и для кого."

BROKEN = "Прогон {run_id} сорвался. Подробности в логе, попробуйте ещё раз."

BROKEN_REDO = (
    "Не получилось продолжить, но запись цела. Пришлите номер ещё раз. (прогон {run_id})"
)

LOST = "Файлы прогона {run_id} не нашлись. Пришлите запись заново."

ORPHANED = (
    "Бот перезапустился, прогон {run_id} прерван на стадии {stage} — пришлите идею заново."
)

# PTB типизирует Application шестью параметрами; здесь важен только сам объект.
BotApplication = Application[Any, Any, Any, Any, Any, Any]

running = asyncio.Lock()


class Ending(BaseModel):
    """Чем кончился прогон: что сказать человеку и каким статусом закрыть строку.

    `stop` заполнен, когда прогон ждёт ответа: и когда встал на выборе, и когда повтор сорвался,
    а ответить ещё раз есть смысл. Статус при этом всегда `awaiting_choice` — строка и есть
    остановка, второго места для неё нет (§4.1).
    """

    text: str
    status: str
    stop: Pause | None = None


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


def progress_text(run: Run, done: list[str]) -> str:
    labels = dict(LABEL)
    if run.audio:
        labels[FIRST_STAGE] = VOICE_INGEST_LABEL
    lines = [f"Прогон {run.run_id}", ""]
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


def cards_published(root: Path) -> int:
    return len(json.loads(journal_of(root / ISSUES_JSON).read_text(encoding="utf-8")))


def finished_text(root: Path) -> str:
    return (
        f"Готово: {cards_published(root)} карточек.\n"
        f"https://trello.com/b/{settings.trello_board_id}"
    )


def demo_run(
    run_id: str, text: str = "", audio: Path | None = None, lang: str = ""
) -> Run:
    """Прогон стенда. Ворота сняты флагом (SPEC §3.2), а не тем, что кнопок ещё нет.

    Язык у нового прогона берётся из настроек, у продолженного — из строки: голосовое уже
    прошло Whisper, и `DEFAULT_LANG` соврал бы про запись, которую бот слышал сам.
    """
    return Run(
        root=RUNS / run_id,
        run_id=run_id,
        lang=lang or settings.default_lang,
        text=text,
        audio=audio,
        auto_approve=True,
    )


def continued(stopped: Stopped) -> Run:
    return demo_run(stopped.run_id, lang=stopped.lang)


def read_candidates(run: Run, artifact: str) -> Candidates:
    return parse_candidates(read_artifact(run.root, artifact))


def permitted(message: Message) -> bool:
    if message.chat_id in allowed_chats():
        return True
    logger.warning("refusal=stranger chat=%s: чата нет в списке разрешённых", message.chat_id)
    return False


async def refuse(message: Message, tag: str, text: str, **facts: object) -> None:
    """Отвечает отказом и оставляет счётную запись.

    Отчёт репетиции (P2-06) отвечает на вопрос «сколько человек упёрлось» числом, а не памятью,
    поэтому у каждого отказа свой tag: `grep -c "refusal=busy"` и есть ответ. Через одну дверь
    ходят все отказы, иначе формат разъедется и считать придётся глазами.
    """
    written = "".join(f" {name}={value}" for name, value in facts.items())
    logger.info("refusal=%s chat=%s%s", tag, message.chat_id, written)
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
    if update.message is None:
        return
    # Замок берётся ради остановки, а не ради приветствия: без него `/start`, посланный во время
    # прогона, снимал пустоту, а прогон в конце записывал остановку обратно.
    async with running:
        await asyncio.to_thread(drop_stop, update.message.chat_id)
    await update.message.reply_text(GREETING)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not message.text or not permitted(message):
        return
    if not message.text.strip():
        await refuse(message, "empty", EMPTY)
        return
    if running.locked():
        await refuse(message, "busy", BUSY)
        return

    async with running:
        stopped = await asyncio.to_thread(waiting_for, message.chat_id)
        if stopped is None:
            run, start, redo = demo_run(new_run_id(), text=message.text), FIRST_STAGE, None
            await asyncio.to_thread(
                start_run, run.run_id, message.chat_id, "text", run.lang, run.auto_approve
            )
        else:
            run = continued(stopped)
            try:
                found = await asyncio.to_thread(read_candidates, run, stopped.artifact)
            except OSError:
                # Файлы прогона мог унести `make clean-runs`: отвечать человеку нечем, и
                # остановка после этого копила бы один и тот же отказ на каждый ответ.
                logger.exception("Прогон %s не нашёл своих файлов", run.run_id)
                await asyncio.to_thread(finish_run, run.run_id, FAILED)
                await message.reply_text(LOST.format(run_id=run.run_id))
                return
            edit = chosen_edit(message.text, found)
            if edit is None:
                await refuse(message, "unknown_number", out_of_range(found))
                return
            # Второй половины воронки в логе не было: `stop=choice` считался, а ответы на него
            # нет, и «сколько человек выбрало» отчёт репетиции (P2-06) взять было неоткуда.
            logger.info(
                "answer=%s run=%s chat=%s",
                "choice" if message.text.strip().isdecimal() else "edit",
                run.run_id,
                message.chat_id,
            )
            start = stopped.stage
            redo = Redo(kind="choice", user_edit=edit, artifact=stopped.artifact)
        note = await message.reply_text(progress_text(run, done_before(start)))
        await follow(note, run, message.date, start, redo)


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or message.voice is None or not permitted(message):
        return
    if too_long(message.voice):
        # Длительность в записи, а не только тег: на репетиции важно, насколько именно
        # переговорили лимит, иначе непонятно, двигать его или оставить.
        await refuse(message, "too_long", TOO_LONG, seconds=voice_seconds(message.voice))
        return
    if running.locked():
        await refuse(message, "busy", BUSY)
        return

    async with running:
        # Голосовое всегда начинает новый прогон и снимает остановку: на стенде следующий
        # человек говорит голосом, и его запись не должна стать правкой к чужому выбору.
        await asyncio.to_thread(drop_stop, message.chat_id)
        run_id = new_run_id()
        audio = RUNS / run_id / VOICE_FILE
        run = demo_run(run_id, audio=audio)
        await asyncio.to_thread(
            start_run, run_id, message.chat_id, "voice", run.lang, run.auto_approve
        )
        # Сообщение о ходе — до скачивания: на конференционном wi-fi голосовое едет секунды,
        # и всё это время человек не должен смотреть в пустой чат.
        note = await message.reply_text(progress_text(run, []))
        try:
            await save_voice(message.voice, audio)
        except TelegramError:
            logger.exception("Прогон %s не забрал голосовое", run_id)
            # Строку закрываем здесь же: незакрытая осталась бы в рабочем статусе навсегда, и
            # следующий старт сказал бы человеку, что прерван прогон, которого не было.
            await asyncio.to_thread(finish_run, run_id, FAILED)
            await note.edit_text(VOICE_NOT_TAKEN)
            return
        await follow(note, run, message.date)


async def on_anything_else(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not permitted(message):
        return
    await refuse(message, "unsupported", UNSUPPORTED)


async def follow(
    note: Message,
    run: Run,
    asked_at: datetime,
    start: str = FIRST_STAGE,
    redo: Redo | None = None,
) -> None:
    """Гонит прогон в потоке, правит одно сообщение до конца и закрывает строку прогона.

    Строку закрывает сюда, а не обработчик: `on_voice` однажды не записал остановку, и выбор
    после голосового ушёл новым прогоном (живой прогон 664534620c9a1c10). Остановка держится,
    пока повтор не удался: сорвись он, человек остался бы и без остановки, и без расшифровки —
    ровно с той потерей, ради которой затевался P3-04.
    """
    done = done_before(start)
    loop = asyncio.get_running_loop()
    progress: list[Future[Message | bool]] = []

    def report(stage: Stage) -> None:
        # Обход идёт в рабочем потоке, а правка сообщения живёт в цикле событий. Строку пишем
        # прямо отсюда: этот поток и так не цикл событий, а язык прогона после ingest назвал
        # Whisper, и до записи он живёт только в памяти обхода (§4.1).
        done.append(stage.name)
        mark_stage(run.run_id, stage.name, run.lang)
        progress.append(
            asyncio.run_coroutine_threadsafe(
                note.edit_text(progress_text(run, done)), loop
            )
        )

    ending = await outcome(run, report, start, redo)
    if ending.stop:
        await asyncio.to_thread(
            stop_on_choice, run.run_id, ending.stop.stage, ending.stop.artifact
        )
    else:
        await asyncio.to_thread(finish_run, run.run_id, ending.status)
    # Правки прогресса ответа Telegram не ждут, поэтому финальная обязана уйти после них: на
    # прогоне 0ac7bdffe0e0ba58 ответ на последнюю правку пришёл вторым и затёр ссылку списком
    # галочек. Прогон выглядел законченным, в логе было чисто, а результата человек не увидел.
    # Сбой правки прогресса финалу не помеха: она косметическая, ссылка — нет.
    for edit in progress:
        with suppress(TelegramError):
            await asyncio.wrap_future(edit)
    await note.edit_text(ending.text)
    # Сколько человек прождал ответа: отчёт репетиции (P2-06) отвечает на этот вопрос числом,
    # а из длительностей стадий его не сложить — между ними скачивание, публикация и правки.
    waited = datetime.now(timezone.utc) - asked_at
    logger.info("run=%s seconds=%d", run.run_id, round(waited.total_seconds()))


def done_before(start: str) -> list[str]:
    """Стадии, пройденные до start: продолженный прогон не показывает их незаконченными."""
    return list(NAMES[: NAMES.index(start)])


def chosen_edit(text: str, found: Candidates) -> str | None:
    """Ответ человека как правка для стадии. None — прислали номер, которого в списке нет."""
    written = text.strip()
    # `isdigit` пускает в `int` то, чего тот не берёт: у «²» он True, а `int("²")` падает, и
    # человек вместо ответа получал тишину.
    if not written.isdecimal():
        return written
    picked = next((idea for idea in found.ideas if idea.number == int(written)), None)
    return f"Выбрана идея {picked.number}: {picked.title}" if picked else None


def out_of_range(found: Candidates) -> str:
    return (
        f"Идей всего {len(found.ideas)}. Пришлите номер от 1 до {len(found.ideas)} "
        "или саму идею словами."
    )


def choice_text(found: Candidates) -> str:
    """Что показать, когда одной идеи не вышло: список из candidates.md.

    Кнопки — P3-07; до них человек присылает номер или идею обычным сообщением.
    """
    listed = [f"{idea.number}. {idea.title}" for idea in found.ideas]
    if found.outcome == "multiple":
        return "\n".join([HEARD, "", *listed, "", PICK_ONE])
    if not listed:
        return "\n".join([NOTHING_HEARD, "", ASK_AGAIN])
    return "\n".join([f"{NOTHING_HEARD} {DISCUSSED}", "", *listed, "", ASK_AGAIN])


def choice_ending(run: Run, stop: Pause) -> Ending:
    """Остановка на выборе, разобранная для человека: список ему и статус строке."""
    found = read_candidates(run, stop.artifact)
    # Ждать ответа есть смысл, только когда есть из чего выбирать: при none в записи не было
    # ничего, и следующее сообщение — новый прогон, а не правка к пустому.
    if found.outcome != "multiple":
        return Ending(text=choice_text(found), status=NO_TASK)
    return Ending(text=choice_text(found), status=AWAITING_CHOICE, stop=stop)


async def outcome(
    run: Run,
    report: Callable[[Stage], None],
    start: str,
    redo: Redo | None,
) -> Ending:
    """Чем кончился прогон: строка человеку и остановка, если от него ждут ответа."""
    # Ответ человеку собирается внутри того же try: и концовка, и выбор читают файл с диска
    # уже после обхода, а сбой такого чтения оставлял человека со списком галочек без
    # концовки — прогон выглядел незаконченным, хотя карточки стояли на доске.
    try:
        waiting = await asyncio.to_thread(walk, run, start, LAST_STAGE, report, redo)
        if waiting and waiting.kind == "choice":
            logger.info("stop=choice run=%s", run.run_id)
            return choice_ending(run, waiting)
        if waiting:
            logger.info("stop=gate run=%s stage=%s", run.run_id, waiting.stage)
            return Ending(
                text=f"Прогон {run.run_id} встал на воротах после стадии {waiting.stage}: "
                "подтвердить их в чате пока нечем.",
                status=FAILED,
            )
        cards = cards_published(run.root)
        logger.info("run=%s finished cards=%d", run.run_id, cards)
        return Ending(text=finished_text(run.root), status=PUBLISHED)
    except NothingHeard as error:
        logger.warning("Прогон %s не услышал в записи ничего: %s", run.run_id, error)
        return Ending(text=str(error), status=NO_TASK)
    except TranscriptionError as error:
        # Текст такой ошибки написан человеку, а не в лог: показываем как есть. Статус `failed`,
        # а не `no_task`: сломанный ffmpeg — это поломка стенда, и отчёт репетиции не должен
        # считать её записью без задания.
        logger.warning("Прогон %s не расшифровал запись: %s", run.run_id, error)
        return Ending(text=str(error), status=FAILED)
    except OSError:
        # Файлы прогона мог унести `make clean-runs` между репетициями. Повторять нечего:
        # остановка после этого копила бы один и тот же отказ на каждый ответ человека.
        logger.exception("Прогон %s не нашёл своих файлов", run.run_id)
        return Ending(text=LOST.format(run_id=run.run_id), status=FAILED)
    except Exception:
        # Единственная точка перехвата на прогон: одно сообщение человеку, одна запись в лог.
        logger.exception("Прогон %s не дошёл до конца", run.run_id)
        if redo:
            # Расшифровка цела, и остановка возвращается на то же место: человек отвечает ещё
            # раз, а не диктует идею заново.
            return Ending(
                text=BROKEN_REDO.format(run_id=run.run_id),
                status=AWAITING_CHOICE,
                stop=Pause(stage=start, artifact=redo.artifact, kind="choice"),
            )
        return Ending(text=BROKEN.format(run_id=run.run_id), status=FAILED)


async def warn_orphans(application: BotApplication) -> None:
    """Прогоны, не пережившие прошлый процесс: закрыть и сказать людям, что они брошены.

    Молчание тут дороже сообщения: человек у стенда ждёт ответа на голосовое, которого больше
    некому дождаться, и без строки бот об этих прогонах вообще ничего не знал.
    """
    for orphan in await asyncio.to_thread(fail_orphans):
        logger.info("orphan run=%s stage=%s chat=%s", orphan.run_id, orphan.stage, orphan.chat_id)
        with suppress(TelegramError):
            await application.bot.send_message(
                orphan.chat_id, ORPHANED.format(run_id=orphan.run_id, stage=orphan.stage)
            )


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
    ensure_schema()

    # Без concurrent_updates бот разбирает обновления по одному и второе сообщение достаёт из
    # очереди только после того, как вернётся обработчик первого, то есть через весь прогон.
    # Отказ «прогон уже идёт» при этом недостижим, а человек три минуты не получает ничего и
    # потом оплачивает свой прогон. Однопрогонность стережёт замок, а не очередь апдейтов.
    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(True)
        .post_init(warn_orphans)
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
    # Время в каждой строке: без него по логу не сказать, сколько заняла публикация и сколько
    # прогон простоял между стадиями, а репетиция спрашивает именно это.
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("app").setLevel(logging.INFO)
    main()
