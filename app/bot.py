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

from pydantic import BaseModel, ConfigDict
from telegram import (
    Audio,
    CallbackQuery,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
    Voice,
)
from sqlalchemy.exc import SQLAlchemyError
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.candidates import Candidates, Idea, parse_candidates
from app.config import ConfigError, LiveApiNotAllowed, MissingApiKey, settings
from app.dialog import budget_spent
from app.ingest import Source, new_run_id
from app.models import IssuesFile
from app.pipeline import (
    BRIEF,
    BRIEF_QUESTION,
    ISSUES_JSON,
    NAMES,
    Stage,
    StopKind,
    after,
    stages_between,
)
from app.publish import journal_of
from app.render import backlog_digest, brief_digest
from app.run import Pause, Redo, Run, read_artifact, walk
from app.store import (
    FAILED,
    NO_TASK,
    REFUSED,
    ensure_schema,
    one_bot_per_database,
    PUBLISHED,
    Stopped,
    add_turn,
    drop_stop,
    fail_orphans,
    finish_run,
    mark_stage,
    start_run,
    stop_run,
    waiting_for,
)
from app.transcribe import (
    FFMPEG_MISSING,
    FFPROBE_MISSING,
    NothingHeard,
    TranscriptionError,
    installed,
    recording_seconds,
)

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
# Запись созвона длиннее голосового, но расшифровывается одним запросом: нарезки ещё нет (P3-03).
MAX_FILE_MINUTES = 20
MAX_FILE_SECONDS = MAX_FILE_MINUTES * 60
# Предел getFile у Bot API: файл крупнее бот скачать не может, и спрашивать о нём согласие незачем.
MAX_FILE_MEGABYTES = 20
MAX_FILE_BYTES = MAX_FILE_MEGABYTES * 1024 * 1024

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
FILE_INGEST_LABEL = "расшифровка записи"
INGEST_LABEL: dict[Source, str] = {"voice": VOICE_INGEST_LABEL, "file": FILE_INGEST_LABEL}

GATES_ON, GATES_OFF = "on", "off"

GATES_STATE = {
    False: "Ворота включены: прогон встанет после брифа и после разбора на задачи.",
    True: "Ворота выключены: прогон идёт до карточек без подтверждений.",
}

GATES_FROM_NEXT_RUN = " Это со следующего прогона, идущий доходит со своим режимом."

GATES_UNKNOWN = (
    "Не понял. /gates on включает ворота, /gates off выключает, /gates показывает, как сейчас."
)

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

UNSUPPORTED = "Принимаю голосовое, текст и запись с диктофона (mp3, m4a, wav)."

VOICE_NOT_TAKEN = "Не смог забрать голосовое из Telegram. Пришлите его ещё раз."

FILE_TOO_BIG = (
    f"Файл больше {MAX_FILE_MEGABYTES} МБ: такой Telegram боту не отдаёт. "
    "Пересохраните запись в mp3 или пришлите её частями."
)

FILE_TOO_LONG = (
    f"Запись длиннее {MAX_FILE_MINUTES} минут я пока не расшифровываю. Пришлите её частями."
)

FILE_NOT_TAKEN = "Не смог забрать запись из Telegram. Пришлите её ещё раз."

# Согласие спрашивается до скачивания (SPEC §3.1): чужая запись без согласия не должна лежать у
# нас даже на диске. Куда уходит запись, сказано в самом вопросе: соглашаются на это, а не на
# абстрактную «обработку».
CONSENT_QUESTION = (
    "Запись уйдёт на расшифровку в OpenAI, за пределы EU. Все, чьи голоса в ней есть, знали о "
    "записи и согласны на это?"
)

CONSENT_YES, CONSENT_NO = "consent:yes", "consent:no"

CONSENT_BUTTONS = ((CONSENT_YES, "Да, все согласны"), (CONSENT_NO, "Нет"))

HEARD = "Вот что я услышал:"

PICK_ONE = (
    "Выберите кнопкой. Можно и словами: напишите, что именно нужно, — я продолжу этот же прогон."
)

NOTHING_HEARD = "Задания в записи я не нашёл."

DISCUSSED = "Вот о чём в ней говорили:"

ASK_AGAIN = "Пришлите идею одним сообщением и чуть подробнее: что нужно сделать и для кого."

BROKEN = "Прогон {run_id} сорвался. Подробности в логе, попробуйте ещё раз."

BROKEN_REDO: dict[StopKind, str] = {
    "choice": "Не получилось продолжить, но запись цела. Ответьте ещё раз — кнопкой или номером."
    " (прогон {run_id})",
    "gate": "Не получилось переделать, но прогон цел. Пришлите правку ещё раз. (прогон {run_id})",
    "answer": "Не получилось продолжить, но прогон цел. Ответьте ещё раз. (прогон {run_id})",
}

# Ворота (SPEC §3.2). Артефакт уходит человеку файлом, в сообщении — то, что о нём можно
# сказать числами, и просьба: любой текст на воротах и есть правка, кнопка её только называет.
GATE_TAIL = "Дальше — кнопкой. Правку пришлите текстом, я переделаю этот шаг."

GATE_EDIT_ASKED = "Пришлите правку одним сообщением: что поменять в этом шаге."

GATE_STOPPED = "Остановил прогон {run_id}. Пришлите новую идею, когда будет готово."

STALE_BUTTON = "Эти кнопки от прогона, который уже не ждёт ответа."

NEXT, EDIT, STOP = "next", "edit", "stop"

# Прогона в строке лога нет: остановку не снимали. Пустое место читается как оборванная
# строка, а прочерк — как ответ.
NO_RUN = "-"

GATE_BUTTONS = ((NEXT, "Дальше"), (EDIT, "Править"), (STOP, "Стоп"))

# brief-диалог (SPEC §3.3). Вопрос уходит человеку сообщением, а не файлом: он на одну строку,
# и отвечать на приложенный документ неудобно.
QUESTION_TAIL = "Ответьте сообщением. Или нажмите «Собирай» — соберу бриф из того, что есть."

ASSEMBLE = "assemble"

ASSEMBLE_BUTTON = "Собирай"

# Правка, которой кончается диалог. Уходит стадии словами, а не одним запретом ветки: запрет
# она узнала бы отказом после лишнего вызова, а сказанное вслух стоит ноль.
ASSEMBLE_ASKED = (
    "Хватит вопросов: собери бриф из того, что уже есть, незакрытое пометь [уточнить: ...]."
)

LOST = "Файлы прогона {run_id} не нашлись. Пришлите запись заново."

ORPHANED = (
    "Бот перезапустился, прогон {run_id} прерван на стадии {stage} — пришлите идею заново."
)

# PTB типизирует Application шестью параметрами; здесь важен только сам объект.
BotApplication = Application[Any, Any, Any, Any, Any, Any]

running = asyncio.Lock()

# Умолчание чата поверх флага из .env: его меняет /gates, а режим идущего прогона живёт в его
# строке (§4.1) и командой не двигается. Память процесса, а не колонка: перезапуск возвращает
# умолчание к флагу, который бот печатает на старте, и терять тут нечего. Заведёт колонку
# P4-03, когда webhook и исполнитель разъедутся по процессам и словарь перестанет быть правдой.
AUTO_APPROVE_BY_CHAT: dict[int, bool] = {}


def auto_approve_for(chat_id: int) -> bool:
    return AUTO_APPROVE_BY_CHAT.get(chat_id, settings.auto_approve)


class Ending(BaseModel):
    """Чем кончился прогон: что сказать человеку и чем закрыть строку.

    `stop` заполнен, когда прогон ждёт ответа: на выборе, на воротах и когда повтор сорвался,
    а ответить ещё раз есть смысл. Статус остановки тогда не пишут: его называет род (§4),
    и второе его написание разошлось бы с первым. `status` — только для концовок без остановки.
    `keyboard` собирает тот, кто читал артефакт: списку кнопок нужны сами идеи, и второе
    чтение файла ради них завело бы второе место, где список живёт.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    text: str
    status: str = ""
    stop: Pause | None = None
    keyboard: InlineKeyboardMarkup | None = None


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
    labels[FIRST_STAGE] = INGEST_LABEL.get(run.source, LABEL[FIRST_STAGE])
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


def started_run(
    run_id: str,
    chat_id: int,
    source: Source = "text",
    text: str = "",
    audio: Path | None = None,
    consent_confirmed: bool | None = None,
) -> Run:
    """Новый прогон из чата. Ворота снимает режим чата (SPEC §3.2), а не отсутствие кнопок.

    Режим чата — флаг из `.env`, пока `/gates` не сказал иначе; умолчание флага — «ворота
    есть» (`CLAUDE.md` §1), и снимает их стенд своим файлом.
    """
    return Run(
        root=RUNS / run_id,
        run_id=run_id,
        lang=settings.default_lang,
        text=text,
        audio=audio,
        source=source,
        consent_confirmed=consent_confirmed,
        auto_approve=auto_approve_for(chat_id),
    )


def continued(stopped: Stopped) -> Run:
    """Продолженный прогон собирается из строки: язык и режим ворот — её факты (§4.1).

    Из настроек их брать нельзя: язык голосовому назвал Whisper, а флаг мог смениться между
    сообщением человека и его ответом, и прогон с воротами доехал бы до доски без них.
    """
    return Run(
        root=RUNS / stopped.run_id,
        run_id=stopped.run_id,
        lang=stopped.lang,
        source=stopped.source,
        consent_confirmed=stopped.consent_confirmed,
        auto_approve=stopped.auto_approve,
    )


def read_candidates(run: Run, artifact: str) -> Candidates:
    return parse_candidates(read_artifact(run.root, artifact))


def permitted(message: Message) -> bool:
    if message.chat_id in allowed_chats():
        return True
    logger.warning("refusal=stranger chat=%s: чата нет в списке разрешённых", message.chat_id)
    return False


async def refuse(
    message: Message, tag: str, text: str, note: Message | None = None, **facts: object
) -> None:
    """Отвечает отказом и оставляет счётную запись.

    Отчёт репетиции (P2-06) отвечает на вопрос «сколько человек упёрлось» числом, а не памятью,
    поэтому у каждого отказа свой tag: `grep -c "refusal=busy"` и есть ответ. Через одну дверь
    ходят все отказы, иначе формат разъедется и считать придётся глазами.

    С `note` отказ правит сообщение о ходе прогона, а не отвечает новым: прогон уже начат, и
    оставленные под отказом галочки читались бы как прогон, который ещё идёт.
    """
    written = "".join(f" {name}={value}" for name, value in facts.items())
    logger.info("refusal=%s chat=%s%s", tag, message.chat_id, written)
    if note is None:
        await message.reply_text(text)
    else:
        await note.edit_text(text)


def reported_seconds(recording: Voice | Audio) -> int:
    # Сегодня PTB отдаёт число при любом входе, и ветка с timedelta недостижима. Она стоит
    # потому, что тип объявлен `int | timedelta`, а с флагом PTB_TIMEDELTA (который станет
    # умолчанием) станет достижимой. Тест на неё написать нечем: флаг читается при импорте.
    duration = recording.duration
    return round(duration.total_seconds()) if isinstance(duration, timedelta) else duration


def too_long(voice: Voice) -> bool:
    return reported_seconds(voice) > MAX_VOICE_SECONDS


async def save_voice(voice: Voice, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    await (await voice.get_file()).download_to_drive(target)


async def save_recording(recording: Audio | Document, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    await (await recording.get_file()).download_to_drive(target)


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not permitted(message):
        return
    # Замок берётся ради остановки, а не ради приветствия: без него `/start`, посланный во время
    # прогона, снимал пустоту, а прогон в конце записывал остановку обратно.
    async with running:
        dropped = await asyncio.to_thread(drop_stop, message.chat_id)
    logger.info("command=start chat=%s dropped=%s", message.chat_id, dropped or NO_RUN)
    await message.reply_text(GREETING)


async def on_gates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ворота включает и выключает ведущий, не гася бота. Идущий прогон это не трогает."""
    message = update.message
    if message is None or not permitted(message):
        return
    asked = (message.text or "").split()[1:]
    if asked and asked[0] not in (GATES_ON, GATES_OFF):
        await refuse(message, "unknown_gates_mode", GATES_UNKNOWN)
        return
    if asked:
        AUTO_APPROVE_BY_CHAT[message.chat_id] = asked[0] == GATES_OFF
    approved = auto_approve_for(message.chat_id)
    logger.info(
        "command=gates chat=%s gates=%s", message.chat_id, GATES_OFF if approved else GATES_ON
    )
    await message.reply_text(GATES_STATE[approved] + (GATES_FROM_NEXT_RUN if asked else ""))


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
            run = started_run(new_run_id(), message.chat_id, text=message.text)
            start, redo = FIRST_STAGE, None
            await asyncio.to_thread(
                start_run,
                run.run_id,
                message.chat_id,
                run.source,
                run.lang,
                run.auto_approve,
                None,
            )
            logger.info("start=text run=%s chat=%s", run.run_id, message.chat_id)
        elif stopped.kind == "gate":
            # Текст на воротах и есть правка: тот же уговор, что и на выборе, и второго
            # состояния ожидания («нажал Править, теперь жду текст») он не заводит.
            run = continued(stopped)
            logger.info("answer=edit run=%s chat=%s", run.run_id, message.chat_id)
            start = stopped.stage
            redo = Redo(kind="gate", user_edit=message.text.strip(), artifact=stopped.artifact)
        elif stopped.kind == "answer":
            run = continued(stopped)
            redo = await brief_answer(message, run, stopped, message.text.strip())
            if redo is None:
                return
            start = stopped.stage
        else:
            run = continued(stopped)
            redo = await choice_answer(message, run, stopped, message.text, "text")
            if redo is None:
                return
            start = stopped.stage
        note = await message.reply_text(progress_text(run, done_before(start)))
        await follow(note, run, message.date, start, redo)


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or message.voice is None or not permitted(message):
        return
    if too_long(message.voice):
        # Длительность в записи, а не только тег: на репетиции важно, насколько именно
        # переговорили лимит, иначе непонятно, двигать его или оставить.
        await refuse(message, "too_long", TOO_LONG, seconds=reported_seconds(message.voice))
        return
    if running.locked():
        await refuse(message, "busy", BUSY)
        return

    async with running:
        # Голосовое всегда начинает новый прогон и снимает остановку: на стенде следующий
        # человек говорит голосом, и его запись не должна стать правкой к чужому выбору.
        dropped = await asyncio.to_thread(drop_stop, message.chat_id)
        run_id = new_run_id()
        audio = RUNS / run_id / VOICE_FILE
        run = started_run(run_id, message.chat_id, source="voice", audio=audio)
        await asyncio.to_thread(
            start_run, run_id, message.chat_id, run.source, run.lang, run.auto_approve, None
        )
        logger.info(
            "start=voice run=%s chat=%s dropped=%s", run_id, message.chat_id, dropped or NO_RUN
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


async def on_gate_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Решение человека на воротах. «Править» только просит текст: правку принимает `on_text`."""
    query, message = update.callback_query, update.effective_message
    if query is None:
        return
    if message is None:
        # Сообщение с кнопкой Telegram отдал недоступным: отвечать человеку некуда, и молчание
        # здесь — единственное место, где нажатие не оставляло бы следа вовсе.
        logger.warning("gate=lost: колбэк пришёл без доступного сообщения")
        await query.answer()
        return
    if not permitted(message):
        return
    # Часы на кнопке крутятся, пока Telegram не получит ответ: снимаем их до всего остального.
    await query.answer()
    pressed = decision_of(query)
    if pressed is None:
        await refuse(message, "stale_button", STALE_BUTTON, run=NO_RUN)
        return
    run_id, stage, decision = pressed
    if running.locked():
        # Решение называется и здесь, хотя до его разбора дело не дойдёт: репетиция 09.09.2026
        # оставила в логе `refusal=busy` от кнопки, и по нему нельзя было сказать, нажали
        # «Дальше», «Править» или «Стоп». Кнопки ворот живут в чате рядом, и разбор упирался
        # в память участников.
        await refuse(message, "busy", BUSY, run=run_id, gate=stage, press=decision)
        return

    async with running:
        stopped = await asyncio.to_thread(waiting_for, message.chat_id)
        if (
            stopped is None
            or stopped.kind != "gate"
            or stopped.run_id != run_id
            or stopped.stage != stage
        ):
            # Решение принимают на конкретных воротах, а не «где-то в этом прогоне». Сообщения
            # с воротами живут в чате вечно, остановку снимают голосовое, `/start` и «Стоп», а
            # прогон за это время уходит к следующим воротам: сверяем и то, и другое.
            await refuse(message, "stale_button", STALE_BUTTON, run=run_id, gate=stage)
            return
        logger.info("gate=%s run=%s chat=%s", decision, run_id, message.chat_id)
        if decision != EDIT:
            # «Править» кнопки не снимает: человек может передумать и подтвердить как есть.
            # Не снявшаяся клавиатура прогон никуда не двинет: кнопка называет свои ворота, и
            # на следующих она уже не пройдёт проверку.
            with suppress(TelegramError):
                await query.edit_message_reply_markup(reply_markup=None)
        if decision == STOP:
            await asyncio.to_thread(drop_stop, message.chat_id)
            await message.reply_text(GATE_STOPPED.format(run_id=run_id))
            return
        if decision == EDIT:
            await message.reply_text(GATE_EDIT_ASKED)
            return
        run = continued(stopped)
        # Со следующей стадии, а не с той, что встала на воротах: подтверждённую переигрывать
        # значит оплатить её второй раз и получить другой артефакт вместо принятого.
        start = after(stopped.stage).name
        note = await message.reply_text(progress_text(run, done_before(start)))
        await follow(note, run, datetime.now(timezone.utc), start)


async def on_choice_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Выбор идеи кнопкой: тот же ответ, что человек набрал бы номером сообщением (SPEC §7.3)."""
    query, message = update.callback_query, update.effective_message
    if query is None:
        return
    if message is None:
        logger.warning("choice=lost: колбэк пришёл без доступного сообщения")
        await query.answer()
        return
    if not permitted(message):
        return
    await query.answer()
    picked = choice_of(query)
    if picked is None:
        await refuse(message, "stale_button", STALE_BUTTON, run=NO_RUN)
        return
    run_id, number = picked
    if running.locked():
        await refuse(message, "busy", BUSY, run=run_id, pick=number)
        return

    async with running:
        stopped = await asyncio.to_thread(waiting_for, message.chat_id)
        if stopped is None or stopped.kind != "choice" or stopped.run_id != run_id:
            # Место остановки здесь не сверяют, в отличие от ворот: выбор у прогона один, и
            # повтор по нему сужает выход intake до `inputs/idea.md` (§7) — вторым списком тот
            # же прогон встать уже не может.
            await refuse(message, "stale_button", STALE_BUTTON, run=run_id)
            return
        run = continued(stopped)
        redo = await choice_answer(message, run, stopped, number, "button")
        if redo is None:
            return
        # Клавиатуру не снимаем, как и «Править» на воротах: сорвавшийся повтор возвращает
        # остановку и просит ответить ещё раз, а снятая отняла бы у человека этот способ.
        note = await message.reply_text(progress_text(run, done_before(stopped.stage)))
        await follow(note, run, datetime.now(timezone.utc), stopped.stage, redo)


async def on_assemble_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Собирай»: диалог брифа кончается, стадия собирает его из того, что уже сказано."""
    query, message = update.callback_query, update.effective_message
    if query is None:
        return
    if message is None:
        logger.warning("brief=lost: колбэк пришёл без доступного сообщения")
        await query.answer()
        return
    if not permitted(message):
        return
    await query.answer()
    run_id = assembling_run(query)
    if run_id is None:
        await refuse(message, "stale_button", STALE_BUTTON, run=NO_RUN)
        return
    if running.locked():
        await refuse(message, "busy", BUSY, run=run_id, brief=ASSEMBLE)
        return

    async with running:
        stopped = await asyncio.to_thread(waiting_for, message.chat_id)
        if stopped is None or stopped.kind != "answer" or stopped.run_id != run_id:
            await refuse(message, "stale_button", STALE_BUTTON, run=run_id)
            return
        logger.info("brief=%s run=%s chat=%s", ASSEMBLE, run_id, message.chat_id)
        # Ход не записывается: человек не ответил на вопрос, а прекратил разговор, и
        # придуманный за него ответ был бы выдуманными данными (CLAUDE.md §5).
        redo = Redo(
            kind="answer",
            user_edit=ASSEMBLE_ASKED,
            artifact=stopped.artifact,
            turns=stopped.turns,
            closes_branch=BRIEF_QUESTION,
        )
        run = continued(stopped)
        # Клавиатуру не снимаем, как и на «Править»: сорвавшийся повтор возвращает остановку.
        note = await message.reply_text(progress_text(run, done_before(stopped.stage)))
        await follow(note, run, datetime.now(timezone.utc), stopped.stage, redo)


async def on_recording(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запись с диктофона: сначала вопрос о согласии, и больше ничего (SPEC §3.1).

    Замок здесь не проверяется: вопрос ничего не стоит, а прогон начинает нажатие, и замок
    проверит оно.
    """
    message = update.message
    if message is None or not permitted(message):
        return
    recording = message.audio or message.document
    if recording is None:
        return
    if recording.file_size is not None and recording.file_size > MAX_FILE_BYTES:
        await refuse(message, "too_big", FILE_TOO_BIG, bytes=recording.file_size)
        return
    if message.audio and reported_seconds(message.audio) > MAX_FILE_SECONDS:
        await refuse(
            message, "file_too_long", FILE_TOO_LONG, seconds=reported_seconds(message.audio)
        )
        return
    # Ответом на сам файл: нажатие берёт запись из этого ответа, а в личном чате PTB без
    # do_quote не цитирует, и файла у кнопки не нашлось бы.
    await message.reply_text(CONSENT_QUESTION, reply_markup=consent_keyboard(), do_quote=True)


async def on_consent_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ответ на вопрос о согласии. Только «Да» скачивает запись и начинает прогон."""
    query, message = update.callback_query, update.effective_message
    if query is None:
        return
    if message is None:
        logger.warning("consent=lost: колбэк пришёл без доступного сообщения")
        await query.answer()
        return
    if not permitted(message):
        return
    await query.answer()
    # Согласие даёт тот, кто нажал, а не тот, кто прислал файл: в группе это разные люди.
    by = query.from_user.id
    if query.data == CONSENT_NO:
        logger.info("consent=no chat=%s by=%s", message.chat_id, by)
        with suppress(TelegramError):
            await query.edit_message_reply_markup(reply_markup=None)
        return
    if query.data != CONSENT_YES:
        await refuse(message, "stale_button", STALE_BUTTON, run=NO_RUN)
        return
    if running.locked():
        # Нажатие называется `press`, а не `consent`: `grep "consent=yes"` считает данные согласия,
        # а при занятом боте его никто не дал.
        await refuse(message, "busy", BUSY, press="yes", by=by)
        return
    sent = message.reply_to_message
    recording = sent and (sent.audio or sent.document)
    if not recording:
        # Файл удалили из чата: скачивать нечего, а в данных кнопки его нет (64 байта).
        await refuse(message, "stale_button", STALE_BUTTON, run=NO_RUN)
        return

    async with running:
        with suppress(TelegramError):
            await query.edit_message_reply_markup(reply_markup=None)
        run_id = new_run_id()
        audio = RUNS / run_id / f"inputs/recording{Path(recording.file_name or '').suffix}"
        run = started_run(
            run_id, message.chat_id, source="file", audio=audio, consent_confirmed=True
        )
        await asyncio.to_thread(
            start_run, run_id, message.chat_id, run.source, run.lang, run.auto_approve, True
        )
        logger.info("start=file run=%s chat=%s", run_id, message.chat_id)
        logger.info("consent=yes chat=%s by=%s run=%s", message.chat_id, by, run_id)
        note = await message.reply_text(progress_text(run, []))
        try:
            await save_recording(recording, audio)
        except TelegramError:
            logger.exception("Прогон %s не забрал запись", run_id)
            await asyncio.to_thread(finish_run, run_id, FAILED)
            await note.edit_text(FILE_NOT_TAKEN)
            return
        try:
            seconds = await asyncio.to_thread(recording_seconds, audio)
        except TranscriptionError as error:
            logger.warning("Прогон %s не узнал длину записи: %s", run_id, error)
            await asyncio.to_thread(finish_run, run_id, FAILED)
            await note.edit_text(str(error))
            return
        if seconds > MAX_FILE_SECONDS:
            # Удаляется при любом KEEP_AUDIO: расшифровывать её никто не будет, а чужая запись
            # не должна лежать у нас дольше, чем нужна.
            audio.unlink()
            await asyncio.to_thread(finish_run, run_id, REFUSED)
            await refuse(
                message, "file_too_long", FILE_TOO_LONG, note=note, run=run_id, seconds=seconds
            )
            return
        # Остановку снимаем только теперь: отвергнутый файл не должен стоить человеку его
        # ответа на воротах.
        dropped = await asyncio.to_thread(drop_stop, message.chat_id)
        logger.info("run=%s dropped=%s", run_id, dropped or NO_RUN)
        await follow(note, run, datetime.now(timezone.utc))


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
        try:
            mark_stage(run.run_id, stage.name, run.lang)
        except SQLAlchemyError:
            # Отметка о стадии — не работа прогона: артефакты уже на диске, карточки будут
            # опубликованы. Ронять из-за неё прогон, который человек оплатил, нельзя; строка
            # останется на прошлой стадии, и её закроет уборка следующего старта.
            logger.exception("Прогон %s не записал стадию %s", run.run_id, stage.name)
        progress.append(
            asyncio.run_coroutine_threadsafe(
                note.edit_text(progress_text(run, done)), loop
            )
        )

    ending = await outcome(run, report, start, redo)
    try:
        if ending.stop:
            await asyncio.to_thread(
                stop_run, run.run_id, ending.stop.kind, ending.stop.stage, ending.stop.artifact
            )
        else:
            await asyncio.to_thread(finish_run, run.run_id, ending.status)
    except SQLAlchemyError:
        # Результат человеку важнее строки: карточки стоят на доске, и молчание вместо ссылки
        # он прочтёт как сорванный прогон. Строку закроет уборка следующего старта.
        logger.exception("Прогон %s не закрыл строку", run.run_id)
    # Правки прогресса ответа Telegram не ждут, поэтому финальная обязана уйти после них: на
    # прогоне 0ac7bdffe0e0ba58 ответ на последнюю правку пришёл вторым и затёр ссылку списком
    # галочек. Прогон выглядел законченным, в логе было чисто, а результата человек не увидел.
    # Сбой правки прогресса финалу не помеха: она косметическая, ссылка — нет.
    for edit in progress:
        with suppress(TelegramError):
            await asyncio.wrap_future(edit)
    await note.edit_text(ending.text, reply_markup=ending.keyboard)
    if ending.stop and ending.stop.kind == "gate":
        # Артефакт целиком уходит файлом: подтвердить то, чего не видел, — не ворота, а кнопка.
        # Но после кнопок и под тем же прикрытием, что и правки прогресса: строка уже стоит в
        # `awaiting_gate`, и сорванная отправка файла оставляла человека с одними галочками —
        # без содержания, без кнопок и без единого способа понять, чего от него ждут.
        with suppress(TelegramError):
            await note.reply_document(run.root / ending.stop.artifact)
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


async def choice_answer(
    message: Message, run: Run, stopped: Stopped, written: str, by: str
) -> Redo | None:
    """Ответ на выбор как повтор стадии. None — повторять нечего, человеку уже сказано.

    Один уговор на два входа: номер кнопкой и то же самое сообщением — один ответ, и
    расходиться им нечем. `by` идёт в лог: кнопку затевали ради того, чтобы у стенда не
    набирали текст, и ответ на «пользовались ли ею» берётся из лога, а не из памяти.
    """
    try:
        found = await asyncio.to_thread(read_candidates, run, stopped.artifact)
    except OSError:
        # Файлы прогона мог унести `make clean-runs`: отвечать человеку нечем, и остановка
        # после этого копила бы один и тот же отказ на каждый ответ.
        logger.exception("Прогон %s не нашёл своих файлов", run.run_id)
        await asyncio.to_thread(finish_run, run.run_id, FAILED)
        await message.reply_text(LOST.format(run_id=run.run_id))
        return None
    edit = chosen_edit(written, found)
    if edit is None:
        await refuse(message, "unknown_number", out_of_range(found), run=run.run_id)
        return None
    # Второй половины воронки в логе не было: `stop=choice` считался, а ответы на него нет, и
    # «сколько человек выбрало» отчёт репетиции (P2-06) взять было неоткуда.
    logger.info(
        "answer=%s by=%s run=%s chat=%s",
        "choice" if written.strip().isdecimal() else "edit",
        by,
        run.run_id,
        message.chat_id,
    )
    return Redo(kind="choice", user_edit=edit, artifact=stopped.artifact)


async def brief_answer(
    message: Message, run: Run, stopped: Stopped, written: str
) -> Redo | None:
    """Ответ на вопрос брифа как повтор стадии. None — повторять нечего, человеку уже сказано.

    Ход записывается до повтора: сообщение в чате прогон не переживёт, а строка переживёт, и
    следующий вопрос стадия задаёт, видя весь разговор. Последний по бюджету ответ кончает
    диалог тем же способом, что и кнопка: спрашивать дальше уже нечего.
    """
    try:
        question = await asyncio.to_thread(read_artifact, run.root, stopped.artifact)
    except OSError:
        # Файлы прогона мог унести `make clean-runs`: отвечать человеку нечем, и остановка
        # после этого копила бы один и тот же отказ на каждый ответ.
        logger.exception("Прогон %s не нашёл своих файлов", run.run_id)
        await asyncio.to_thread(finish_run, run.run_id, FAILED)
        await message.reply_text(LOST.format(run_id=run.run_id))
        return None
    try:
        await asyncio.to_thread(add_turn, run.run_id, question, written)
    except SQLAlchemyError:
        # Потерянный ход стоит повторённого вопроса, а поднятое исключение — всего прогона,
        # за который человек уже заплатил четырьмя стадиями.
        logger.exception("Прогон %s не записал ход диалога", run.run_id)
    asked = len(stopped.turns) + 1
    over = budget_spent(asked)
    logger.info(
        "brief=%s asked=%d run=%s chat=%s",
        ASSEMBLE if over else "answer",
        asked,
        run.run_id,
        message.chat_id,
    )
    return Redo(
        kind="answer",
        user_edit=f"{written}\n\n{ASSEMBLE_ASKED}" if over else written,
        artifact=stopped.artifact,
        turns=stopped.turns,
        closes_branch=BRIEF_QUESTION if over else None,
    )


def out_of_range(found: Candidates) -> str:
    return (
        f"Идей всего {len(found.ideas)}. Пришлите номер от 1 до {len(found.ideas)} "
        "или саму идею словами."
    )


def choice_text(found: Candidates) -> str:
    """Что показать, когда одной идеи не вышло: список из candidates.md.

    Номера в списке и на кнопках под ним — одни и те же: кнопка отвечает за человека ровно то,
    что он набрал бы сам. Словами ответить по-прежнему можно, и это уже не номер, а правка.
    """
    listed = [f"{idea.number}. {idea.title}" for idea in found.ideas]
    if found.outcome == "multiple":
        return "\n".join([HEARD, "", *listed, "", PICK_ONE])
    if not listed:
        return "\n".join([NOTHING_HEARD, "", ASK_AGAIN])
    return "\n".join([f"{NOTHING_HEARD} {DISCUSSED}", "", *listed, "", ASK_AGAIN])


def assemble_keyboard(run_id: str) -> InlineKeyboardMarkup:
    # Кнопка называет прогон, но не место остановки, в отличие от воротной: вопрос у прогона
    # один за раз, и повтор по нему сужает выход стадии до брифа — вторым вопросом тот же
    # прогон встать уже не может.
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(ASSEMBLE_BUTTON, callback_data=f"{ASSEMBLE}:{run_id}")]]
    )


def consent_keyboard() -> InlineKeyboardMarkup:
    # Запись в данных кнопки не лежит: file_id не влезает в 64 байта callback_data. Её берут из
    # сообщения, на которое отвечает вопрос.
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(title, callback_data=data) for data, title in CONSENT_BUTTONS]]
    )


def gate_keyboard(run_id: str, stage: str) -> InlineKeyboardMarkup:
    # Кнопка называет и прогон, и ворота, на которых она выросла. Одного прогона мало: «Править»
    # кнопок не снимает, поэтому у прогона в чате остаётся живое сообщение прошлых ворот, и
    # нажатое на нём «Дальше» уводило обход со стадии, где прогон стоит сейчас, — то есть
    # публиковало бэклог, которого никто не подтверждал.
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(title, callback_data=f"gate:{run_id}:{stage}:{decision}")
                for decision, title in GATE_BUTTONS
            ]
        ]
    )


def choice_keyboard(run_id: str, ideas: list[Idea]) -> InlineKeyboardMarkup:
    # На кнопке только номер: название стоит строкой выше в том же сообщении, а на кнопке его
    # обрезала бы ширина экрана неизвестно где. Ворота своим кнопкам называют ещё и стадию,
    # выбору называть нечего: он у прогона один.
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(str(idea.number), callback_data=f"pick:{run_id}:{idea.number}")
                for idea in ideas
            ]
        ]
    )


def backlog_of(run: Run) -> str:
    return backlog_digest(IssuesFile.model_validate_json(read_artifact(run.root, ISSUES_JSON)))


# Ворота объявляет список стадий (`gate_after`), а короткое содержание пишет код: третьи
# ворота, заведённые данными, обязаны упереться здесь, а не показать человеку «Бриф готов.»
# над чужим артефактом.
GATE_DIGESTS: dict[str, Callable[[Run], str]] = {
    "brief": lambda run: brief_digest(read_artifact(run.root, BRIEF)),
    "decompose": backlog_of,
}


def gate_text(run: Run, stop: Pause) -> str:
    """Что сказать о готовом артефакте: числа из него самого, а он уходит следом файлом."""
    digest = GATE_DIGESTS.get(stop.stage)
    if digest is None:
        raise ValueError(f"у ворот после {stop.stage} нет краткого содержания")
    return f"{digest(run)}\n\n{GATE_TAIL}"


def button_of(query: CallbackQuery, fields: int) -> list[str] | None:
    """Поля из данных кнопки. None — кнопка не той формы.

    Данные писал сам бот, и разбирать их как чужой ввод незачем — но сообщения переживают
    выкладку, а формат кнопки уже менялся однажды. Нажатая кнопка прошлой формы обязана
    получить тот же отказ, что и протухшая, а не уронить обработчик молчанием в ответ.
    """
    parts = (query.data or "").split(":")
    return parts if len(parts) == fields else None


def question_text(run: Run, stop: Pause) -> str:
    """Вопрос стадии как он есть плюс просьба ответить: пересказывать его нечем."""
    return f"{read_artifact(run.root, stop.artifact).strip()}\n\n{QUESTION_TAIL}"


def decision_of(query: CallbackQuery) -> tuple[str, str, str] | None:
    """Прогон, ворота и решение из данных кнопки."""
    parts = button_of(query, 4)
    return (parts[1], parts[2], parts[3]) if parts else None


def choice_of(query: CallbackQuery) -> tuple[str, str] | None:
    """Прогон и номер идеи из данных кнопки. Нечисловой номер ушёл бы в стадию правкой из мусора."""
    parts = button_of(query, 3)
    return (parts[1], parts[2]) if parts and parts[2].isdecimal() else None


def assembling_run(query: CallbackQuery) -> str | None:
    """Прогон, которому сказали кончать спрашивать."""
    parts = button_of(query, 2)
    return parts[1] if parts else None


def choice_ending(run: Run, stop: Pause) -> Ending:
    """Остановка на выборе, разобранная для человека: список ему и статус строке."""
    found = read_candidates(run, stop.artifact)
    # Ждать ответа есть смысл, только когда есть из чего выбирать: при none в записи не было
    # ничего, и следующее сообщение — новый прогон, а не правка к пустому.
    if found.outcome != "multiple":
        return Ending(text=choice_text(found), status=NO_TASK)
    return Ending(
        text=choice_text(found), stop=stop, keyboard=choice_keyboard(run.run_id, found.ideas)
    )


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
        if waiting and waiting.kind == "answer":
            logger.info("stop=answer run=%s", run.run_id)
            return Ending(
                text=question_text(run, waiting),
                stop=waiting,
                keyboard=assemble_keyboard(run.run_id),
            )
        if waiting:
            logger.info("stop=gate run=%s stage=%s", run.run_id, waiting.stage)
            return Ending(
                text=gate_text(run, waiting),
                stop=waiting,
                keyboard=gate_keyboard(run.run_id, waiting.stage),
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
                text=BROKEN_REDO[redo.kind].format(run_id=run.run_id),
                stop=Pause(stage=start, artifact=redo.artifact, kind=redo.kind),
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
    # Ключ, ffmpeg и ffprobe — на старте по одной причине: без любого из них запись не
    # расшифровать, а узнать об этом на первой записи значит получить «прогон сорвался».
    if not settings.openai_api_key:
        raise MissingApiKey(
            "OPENAI_API_KEY не задан, а без него голосовое не расшифровать. "
            "Скопируйте .env.example в .env и заполните."
        )
    if not installed("ffmpeg"):
        raise ConfigError(FFMPEG_MISSING)
    if not installed("ffprobe"):
        raise ConfigError(FFPROBE_MISSING)
    ensure_schema()
    # Режим прогона — в лог одной строкой: утренний чек-лист стенда спрашивает, сняты ли
    # ворота, и ответ на это не должен зависеть от памяти о содержимом .env.
    logger.info("auto_approve=%s", settings.auto_approve)

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
    application.add_handler(CommandHandler("gates", on_gates))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    application.add_handler(MessageHandler(filters.VOICE, on_voice))
    application.add_handler(
        MessageHandler(filters.AUDIO | filters.Document.AUDIO, on_recording)
    )
    application.add_handler(CallbackQueryHandler(on_gate_button, pattern=r"^gate:"))
    application.add_handler(CallbackQueryHandler(on_choice_button, pattern=r"^pick:"))
    application.add_handler(CallbackQueryHandler(on_assemble_button, pattern=rf"^{ASSEMBLE}:"))
    application.add_handler(CallbackQueryHandler(on_consent_button, pattern=r"^consent:"))
    # Последним и почти без фильтра по типу: молчание в ответ на присланный файл или на опечатку
    # в команде человек у стенда читает как поломку бота. /start сюда не доходит, его забирает
    # обработчик выше. Служебные события чата (кто-то вошёл, сменилось название) под отказ не
    # попадают — им никто ничего не присылал.
    application.add_handler(MessageHandler(~filters.StatusUpdate.ALL, on_anything_else))
    with one_bot_per_database() as alone:
        if not alone:
            raise ConfigError(
                "На этой базе уже работает бот. Погасите его или укажите другую DATABASE_URL: "
                "два бота на одну базу считают прогоны друг друга брошенными."
            )
        application.run_polling()


if __name__ == "__main__":
    # Время в каждой строке: без него по логу не сказать, сколько заняла публикация и сколько
    # прогон простоял между стадиями, а репетиция спрашивает именно это.
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("app").setLevel(logging.INFO)
    main()
