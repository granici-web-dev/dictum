"""Telegram-бот: запись, голосовое или текст → разбор встречи, а кнопками под ним поручение или
вся запись как идея до карточек. См. SPEC.md §7.3.

Единственный асинхронный модуль (CONVENTIONS): пайплайн синхронный и уходит в поток, а обратно
докладывает через цикл событий. Решения, которые можно принять без Telegram, вынесены функциями —
обвес обработчиков тестировать незачем.
"""

import asyncio
import json
import logging
import unicodedata
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

from app.answers import Answers
from app.candidates import Candidates, Idea, parse_candidates
from app.clarify import Clarify, has_questions
from app.config import ConfigError, LiveApiNotAllowed, MissingApiKey, settings
from app.dialog import budget_spent
from app.ingest import Source, new_run_id
from app.models import IssuesFile
from app.pipeline import (
    ANSWERS,
    BRIEF,
    BRIEF_QUESTION,
    CLARIFY_JSON,
    ISSUES_JSON,
    NAMES,
    PROJECT,
    REVIEW_JSON,
    REVIEW_MD,
    STEPS_JSON,
    TRANSCRIPT,
    Stage,
    StopKind,
    after,
    route_end,
    route_of,
    stages_between,
)
from app.project import ProjectContextError, read_project
from app.publish import ASSIGNMENTS_LIST, PublishedCard, journal_of
from app.render import (
    backlog_digest,
    brief_digest,
    questions_copy_text,
    questions_note,
    review_lead,
    review_messages,
    standards_line,
    standards_snapshot_line,
    steps_copy_text,
    steps_digest,
)
from app.review import Review
from app.run import Pause, Redo, Run, current_standards, read_artifact, walk
from app.steps import Steps
from app.store import (
    DROPPED,
    FAILED,
    NO_TASK,
    QUESTIONS_SENT,
    REFUSED,
    ensure_schema,
    one_bot_per_database,
    PUBLISHED,
    REVIEWED,
    Parent,
    Parked,
    Stopped,
    add_turn,
    child_of,
    drop_stop,
    fail_orphans,
    finish_run,
    mark_stage,
    note_questions,
    parent_of,
    park_run,
    parked_by_reply,
    reopen_run,
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
TASK_ROUTE_START, TASK_ROUTE_END = "assignment", "card"
CLARIFY_STAGE, ANSWERS_STAGE, STEPS_STAGE = "clarify", "answers", "steps"
IDEA_ROUTE_START = "handoff"
# Журнал публикации лежит рядом с файлом, который путь кладёт на доску: у каждого пути свой.
BOARD_CONTRACT = {TASK_ROUTE_START: STEPS_JSON, IDEA_ROUTE_START: ISSUES_JSON}
VOICE_FILE = "inputs/voice.oga"

# TODO(P3-09): одна граница длины для голосового и файла.
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
    "ingest": "принял текст",
    "review": "разбор встречи",
    "handoff": "взял расшифровку разбора",
    "intake": "выделил суть",
    "brief": "собрал бриф",
    "research": "ресёрч пропущен",
    "prd": "написал PRD",
    "decompose": "разбил на задачи",
    "publish": "опубликовал в Trello",
    "assignment": "поручение из разбора",
    "clarify": "вопросы для тимлида",
    "answers": "ответы тимлида",
    "steps": "разложил на шаги",
    "card": "карточка в Trello",
}

# Голосовое расшифровывается на той же стадии, что принимает текст, а метка стадии живёт в
# сообщении и с «▸», и с «✓»: отглагольное существительное читается верно в обоих.
VOICE_INGEST_LABEL = "расшифровка голосового"
FILE_INGEST_LABEL = "расшифровка записи"
INGEST_LABEL: dict[Source, str] = {"voice": VOICE_INGEST_LABEL, "file": FILE_INGEST_LABEL}

GATES_ON, GATES_OFF = "on", "off"

GATES_STATE = {
    False: (
        "Ворота включены. Своя идея встанет на вопросы брифа, на бриф и на задачи, поручение "
        "встанет на вопросы для тимлида и на шаги, прежде чем попасть на доску."
    ),
    True: (
        "Ворота выключены. Идея и поручение идут до доски без подтверждений, бриф без вопросов, "
        "а вопросы для тимлида уходят на карточку открытыми."
    ),
}

GATES_FROM_NEXT_RUN = " Это со следующего прогона, идущий доходит со своим режимом."

GATES_UNKNOWN = (
    "Не понял. /gates on включает ворота, /gates off выключает, /gates показывает, как сейчас."
)

GREETING = (
    "Пришлите запись встречи файлом, голосовое или текст. Я выпишу все поручения: кто поручил, "
    "срок, что не надо и что переспросить, со сказанным в оригинале и переводом.\n"
    "Кнопка «Разложить на шаги» под поручением сначала соберёт вопросы для тимлида: отберите "
    "нужные и отправьте их сами, а его ответ пришлите ответом (reply) на моё сообщение. Потом "
    "разложу поручение на шаги и положу карточкой в Trello.\n"
    "Кнопка «Проработать как идею» под оглавлением разбора проведёт всю запись путём своей идеи: "
    "вопросы, бриф, задачи и карточки на доске идей.\n"
    "Это займёт пару минут, я буду писать после каждого шага."
)

BUSY = "Прогон уже идёт, дождитесь его конца."

EMPTY = "Пустое сообщение. Пришлите текст словами."

TOO_LONG = (
    f"Голосовое длиннее {MAX_VOICE_MINUTES} минут я пока не расшифровываю. "
    "Наговорите покороче или пришлите текстом."
)

UNSUPPORTED = "Принимаю голосовое, текст и аудиофайл с записью, например mp3, m4a или wav."

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

CONSENT_ANSWERED_AT = "%d.%m %H:%M UTC"

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

# Короткое согласие текстом на воротах шагов (P3-11). За нажатие его принять нельзя: ворота
# существуют ровно против публикации без явного решения, а список фраз промахнётся на «ок,
# только…» и положит карточку, которой никто не подтверждал. Правкой тоже нельзя: вызов модели
# за «passt» стоит $0.03–0.06 и возвращает те же шаги.
APPROVALS = frozenset(
    {
        "ok",
        "ок",
        "окей",
        "всё ок",
        "все ок",
        "passt",
        "passt so",
        "alles gut",
        "sieht gut aus",
        "go",
    }
)

APPROVAL_TEXT = "Похоже на согласие. Нажмите «Дальше» выше, и карточка ляжет на доску."

GATE_STOPPED = "Остановил прогон {run_id}."

STALE_BUTTON = "Эти кнопки от прогона, который уже не ждёт ответа."

# Новая запись остановку не снимает: у одного человека остановка всегда его собственная и уже
# оплачена. «Стоп» не назван: он есть только на воротах, а /start снимает остановку любого рода.
STOP_ALIVE = (
    "Прогон {run_id} ждёт вашего ответа выше. Ответьте там или пришлите /start, чтобы его снять, "
    "затем повторите."
)

TASK_BUTTON = "Разложить на шаги"

# Вопросы для тимлида (P3-11). Прогон не занимает места остановки: пока тимлид молчит, чат
# принимает новые записи и другие поручения, поэтому сообщение о ходе прогона говорит об этом
# вслух — иначе парковка неотличима от повисшего прогона.
QUESTIONS_PARKED = (
    "Вопросы для тимлида ниже. Прогон {run_id} ждёт ответа и чат не занимает: можно прислать "
    "новую запись или взять другое поручение."
)

STALE_REPLY = (
    "Вопросы по поручению {number} уже закрыты, прогон {run_id} идёт дальше без этого ответа."
)

REPLY_LOST = (
    "Прогон {run_id} по поручению {number} больше не ждёт ответа: разбора, из которого он вырос, "
    "у меня нет. Нажмите «Разложить на шаги» ещё раз."
)

QUESTIONS_WITHOUT, QUESTIONS_STOP = "without", "stop"

QUESTIONS_BUTTONS = (
    (QUESTIONS_WITHOUT, "Продолжить без ответов"),
    (QUESTIONS_STOP, "Стоп"),
)

IDEA = "idea"

IDEA_BUTTON = "Проработать как идею"

# Отказ до замка и до строки: без доски путь идеи оплатил бы бриф, PRD и декомпозицию, чтобы
# упасть на публикации.
IDEA_BOARD_MISSING = (
    "Доска для идей не настроена: впишите TRELLO_IDEA_BOARD_ID в .env и перезапустите бота. "
    "Без неё путь идеи дойдёт до публикации и там упадёт."
)

PARENT_GONE = (
    "Файлы этого разбора удалены с диска, кнопка больше не работает. Пришлите запись заново."
)

ALREADY_PUBLISHED = "Поручение {number} уже на доске: карточка {key}.\n{url}"

TASK_FILES_GONE = (
    "Поручение {number} уже на доске, но файлы его прогона удалены с диска, и ссылки на карточку "
    "у меня нет. Ищите её в списке Assignments по строке run:{run_id} в описании."
)

IDEA_ALREADY_PUBLISHED = (
    "Эта запись уже проработана как идея, прогон {run_id}: карточки на доске идей.\n{url}"
)

CARD_FINISHED = f"Готово: карточка {{key}} в списке {ASSIGNMENTS_LIST}.\n{{url}}"

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
    "Бот перезапустился, прогон {run_id} прерван на стадии {stage}. Пришлите запись заново или "
    "нажмите кнопку под разбором ещё раз."
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
    `parked` — вопросы для тимлида отданы владельцу (P3-11): прогон ждёт днями и места остановки
    в чате не занимает, поэтому это не `stop` и статус ему пишет `park_run`, а не `Ending`.
    `keyboard` собирает тот, кто читал артефакт: списку кнопок нужны сами идеи, и второе
    чтение файла ради них завело бы второе место, где список живёт. `review` — по тому же
    доводу: поручения уходят сообщениями из того разбора, чьё оглавление уже в `text`.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    text: str
    status: str = ""
    stop: Pause | None = None
    parked: bool = False
    keyboard: InlineKeyboardMarkup | None = None
    review: Review | None = None


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


def progress_text(run: Run, done: list[str], start: str = FIRST_STAGE) -> str:
    labels = dict(LABEL)
    labels[FIRST_STAGE] = INGEST_LABEL.get(run.source, LABEL[FIRST_STAGE])
    lines = [f"Прогон {run.run_id}", ""]
    marked_current = False
    for stage in stages_between(*route_of(start)):
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


def idea_board_url() -> str:
    return f"https://trello.com/b/{settings.trello_idea_board_id}"


def finished_text(root: Path) -> str:
    return f"Готово: {cards_published(root)} карточек.\n{idea_board_url()}"


def task_card(root: Path) -> PublishedCard:
    """Карточка поручения из журнала его прогона: ключ `A<N>` в журнале один."""
    journal = json.loads(journal_of(root / STEPS_JSON).read_text(encoding="utf-8"))
    (card,) = journal.values()
    return PublishedCard.model_validate(card)


def started_run(
    run_id: str,
    chat_id: int,
    source: Source,
    text: str = "",
    audio: Path | None = None,
    consent_confirmed: bool | None = None,
) -> Run:
    """Новый прогон из чата. Ворота снимает режим чата (SPEC §3.2), а не отсутствие кнопок.

    Режим чата — флаг из `.env`, пока `/gates` не сказал иначе; умолчание флага — «ворота
    есть» (`CLAUDE.md` §1), и снимает их явный true в `.env`.
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


def child_run(
    run_id: str,
    parent: Parent,
    number: int | None,
    auto_approve: bool,
    answers: Answers | None = None,
) -> Run:
    """Прогон по выбору из разбора: поручение `number` или, при None, вся запись как идея.

    Язык и источник записи — факты родителя (§4.1). Вопросы брифа и вопросы для тимлида идут там
    же, где ворота: кто готов подтверждать, готов и отвечать, и готов отбирать вопросы (§3.2).
    `answers` несёт продолженный прогон: ответ тимлида или отказ его ждать.
    """
    return Run(
        root=RUNS / run_id,
        run_id=run_id,
        lang=parent.lang,
        source=parent.source,
        auto_approve=auto_approve,
        interactive=not auto_approve,
        parent_root=RUNS / parent.run_id,
        parent_run_id=parent.run_id,
        assignment=number,
        asks_teamlead=not auto_approve,
        answers=answers,
    )


def resumed_start(root: Path, first: str) -> str:
    """Откуда вести сорванный прогон, начатый кнопкой: по цепочке артефактов, что он успел.

    Журнал рядом с файлом для доски значит, что публикация начиналась: карточки могут стоять на
    доске, и собирать их заново значило бы оплатить стадии второй раз и дособирать карточки
    другим набором. Лежащий `answers.md` значит, что тимлида уже спросили и ответ получен:
    начать с `assignment` значило бы спросить его заново и выбросить ответ, которого ждали днями.
    Без того и другого до доски дело не дошло, и маршрут идёт с первой стадии.
    """
    if journal_of(root / BOARD_CONTRACT[first]).exists():
        return route_end(first)
    if first == TASK_ROUTE_START and (root / ANSWERS).exists():
        return after(ANSWERS_STAGE).name
    return first


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
        interactive=not stopped.auto_approve,
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

    Отчёт серии живых прогонов (P2-06) отвечает на вопрос «сколько человек упёрлось» числом, а
    не памятью, поэтому у каждого отказа свой tag: `grep -c "refusal=busy"` и есть ответ. Через
    одну дверь ходят все отказы, иначе формат разъедется и считать придётся глазами.

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
    """Ворота включает и выключает человек, не гася бота. Идущий прогон это не трогает."""
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
        # Ответ тимлида разбирается раньше остановки: reply на вопросы новой встречей не бывает,
        # и разбор такого текста стоил бы денег, а ответа прогон так и не дождался бы.
        replied = message.reply_to_message
        parked = (
            await asyncio.to_thread(parked_by_reply, message.chat_id, replied.message_id)
            if replied is not None
            else None
        )
        if parked is not None:
            await answer_teamlead(message, parked)
            return
        stopped = await asyncio.to_thread(waiting_for, message.chat_id)
        if stopped is None:
            run = started_run(new_run_id(), message.chat_id, source="text", text=message.text)
            start, redo = FIRST_STAGE, None
            await asyncio.to_thread(
                start_run,
                run.run_id,
                message.chat_id,
                run.source,
                run.lang,
                run.auto_approve,
                run.consent_confirmed,
                FIRST_STAGE,
            )
            logger.info("start=text run=%s chat=%s", run.run_id, message.chat_id)
        elif stopped.kind == "gate":
            if stopped.stage == STEPS_STAGE and sounds_like_approval(message.text):
                # Остановка остаётся на месте: человеку сказано, чем подтвердить, а стадия не
                # вызвана — повтор вернул бы те же шаги за те же деньги.
                await refuse(message, "approval_text", APPROVAL_TEXT, run=stopped.run_id)
                return
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
        note = await message.reply_text(progress_text(run, done_before(start), start))
        await follow(note, run, message.date, start, redo)


async def answer_teamlead(message: Message, parked: Parked) -> None:
    """Текст ответом (reply) на вопросы поручения: тот же прогон идёт дальше со стадии `answers`.

    Содержимое берётся как есть, целиком: какие вопросы оно закрывает, читает стадия шагов по
    смыслу, а не код по номерам — владелец отправляет не все вопросы, а тимлид нумерует по-своему.
    """
    if parked.status != QUESTIONS_SENT:
        await refuse(
            message,
            "stale_reply",
            STALE_REPLY.format(number=parked.assignment, run_id=parked.run_id),
            run=parked.run_id,
            task=parked.assignment,
        )
        return
    stopped = await asyncio.to_thread(waiting_for, message.chat_id)
    if stopped is not None:
        # Иначе припаркованный прогон дошёл бы до своих ворот и упёрся в индекс одной остановки
        # уже после оплаченных стадий.
        await refuse(
            message,
            "stop_alive",
            STOP_ALIVE.format(run_id=stopped.run_id),
            run=stopped.run_id,
            task=parked.assignment,
        )
        return
    written = message.text or ""
    logger.info(
        "answer=teamlead run=%s chat=%s chars=%d", parked.run_id, message.chat_id, len(written)
    )
    answers = Answers(status="answered", received_at=message.date, text=written)
    await resume_parked(message, parked, answers)


async def resume_parked(message: Message, parked: Parked, answers: Answers) -> None:
    """Припаркованный прогон идёт дальше: ответ тимлида или отказ его ждать ложится в `answers.md`.

    Язык и режим ворот берутся из строк разбора и ребёнка, как при первом нажатии: прогон
    простоял дни, и настройки за это время могли смениться.
    """
    parent = await asyncio.to_thread(parent_of, parked.parent_id, message.chat_id)
    child = await asyncio.to_thread(child_of, parked.parent_id, parked.assignment)
    if parent is None or child is None:
        await refuse(
            message,
            "stale_reply",
            REPLY_LOST.format(run_id=parked.run_id, number=parked.assignment),
            run=parked.run_id,
            parent="gone",
        )
        return
    run = child_run(parked.run_id, parent, parked.assignment, child.auto_approve, answers)
    await asyncio.to_thread(reopen_run, run.run_id, ANSWERS_STAGE)
    note = await message.reply_text(
        progress_text(run, done_before(ANSWERS_STAGE), ANSWERS_STAGE)
    )
    await follow(note, run, message.date, ANSWERS_STAGE)


async def send_questions(answering: Message, run_id: str, root: Path, number: int) -> None:
    """Вопросы владельцу двумя сообщениями ответом на нажатое поручение (SPEC §7.3).

    Первое — текст для копирования: только вопросы на языке встречи, без подписей бота и без
    кнопок, потому что копируемое не должно нести ничего лишнего. Второе — перевод, зачем
    спрашивать, обращение, стандарты проекта и две кнопки.
    """
    clarify = Clarify.model_validate_json(read_artifact(root, CLARIFY_JSON))
    project = read_project(read_artifact(root, PROJECT))
    sent: list[int] = []
    for written, keyboard in (
        (questions_copy_text(clarify), None),
        (questions_note(clarify, number, project), questions_keyboard(run_id)),
    ):
        # Каждая отправка прикрыта, как у разбора: сорванная не должна отнять вторую.
        with suppress(TelegramError):
            posted = await answering.reply_text(written, reply_markup=keyboard, do_quote=True)
            sent.append(posted.message_id)
    if len(sent) < 2:
        # Строка уже `questions_sent`, и лечит это повторное нажатие кнопки поручения: оно
        # пришлёт те же вопросы из `clarify.json`, не платя за стадию второй раз.
        logger.warning(
            "questions=kept run=%s chat=%s: сообщения не ушли, нажмите поручение ещё раз",
            run_id,
            answering.chat_id,
        )
        return
    await asyncio.to_thread(note_questions, run_id, sent)


async def on_questions_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки под переводом вопросов: идти без ответов или снять прогон (SPEC §7.3)."""
    query, message = update.callback_query, update.effective_message
    if query is None:
        return
    if message is None:
        logger.warning("questions=lost: колбэк пришёл без доступного сообщения")
        await query.answer()
        return
    if not permitted(message):
        return
    await query.answer()
    pressed = asked_of(query)
    if pressed is None:
        await refuse(message, "stale_button", STALE_BUTTON, run=NO_RUN)
        return
    run_id, decision = pressed
    if running.locked():
        await refuse(message, "busy", BUSY, run=run_id, press=decision)
        return

    async with running:
        # Прогон ищется по самому сообщению с кнопками: его id и записан в строке, а кнопке
        # остаётся сверить, тот ли это прогон и ждёт ли он ещё.
        parked = await asyncio.to_thread(parked_by_reply, message.chat_id, message.message_id)
        if parked is None or parked.run_id != run_id or parked.status != QUESTIONS_SENT:
            await refuse(message, "stale_button", STALE_BUTTON, run=run_id)
            return
        stopped = await asyncio.to_thread(waiting_for, message.chat_id)
        if stopped is not None:
            await refuse(
                message,
                "stop_alive",
                STOP_ALIVE.format(run_id=stopped.run_id),
                run=stopped.run_id,
                task=parked.assignment,
            )
            return
        logger.info("questions=%s run=%s chat=%s", decision, run_id, message.chat_id)
        # Оба решения кончают ожидание, поэтому кнопки уходят: оставленные звали бы нажать ещё
        # раз прогон, который уже идёт дальше.
        with suppress(TelegramError):
            await query.edit_message_reply_markup(reply_markup=None)
        if decision == QUESTIONS_STOP:
            await asyncio.to_thread(finish_run, run_id, DROPPED)
            await message.reply_text(GATE_STOPPED.format(run_id=run_id))
            return
        await resume_parked(message, parked, Answers(status="without_answers"))


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or message.voice is None or not permitted(message):
        return
    if too_long(message.voice):
        # Длительность в записи, а не только тег: по логу видно, насколько именно
        # переговорили лимит, иначе непонятно, двигать его или оставить.
        await refuse(message, "too_long", TOO_LONG, seconds=reported_seconds(message.voice))
        return
    if running.locked():
        await refuse(message, "busy", BUSY)
        return

    async with running:
        stopped = await asyncio.to_thread(waiting_for, message.chat_id)
        if stopped is not None:
            await refuse(
                message, "stop_alive", STOP_ALIVE.format(run_id=stopped.run_id), run=stopped.run_id
            )
            return
        run_id = new_run_id()
        audio = RUNS / run_id / VOICE_FILE
        run = started_run(run_id, message.chat_id, source="voice", audio=audio)
        await asyncio.to_thread(
            start_run,
            run_id,
            message.chat_id,
            run.source,
            run.lang,
            run.auto_approve,
            run.consent_confirmed,
            FIRST_STAGE,
        )
        logger.info("start=voice run=%s chat=%s", run_id, message.chat_id)
        # Сообщение о ходе — до скачивания: на мобильной сети голосовое едет секунды,
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
        # Решение называется и здесь, хотя до его разбора дело не дойдёт: серия живых прогонов
        # 09.09.2026 оставила в логе `refusal=busy` от кнопки, и по нему нельзя было сказать, нажали
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
        note = await message.reply_text(progress_text(run, done_before(start), start))
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
        note = await message.reply_text(
            progress_text(run, done_before(stopped.stage), stopped.stage)
        )
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
        note = await message.reply_text(
            progress_text(run, done_before(stopped.stage), stopped.stage)
        )
        await follow(note, run, datetime.now(timezone.utc), stopped.stage, redo)


async def on_child_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки под разбором: поручение до карточки или вся запись путём идеи (SPEC §7.3).

    Оба пути после разбора одинаково ребёнок разбора, поэтому порядок отказов, возобновление и
    индексы у них одни. Нажатие не снимает клавиатуру: сообщения разбора живут в чате вечно, и
    повторное нажатие отвечает по состоянию прогона, а не по тому, осталась ли кнопка.
    """
    query, message = update.callback_query, update.effective_message
    if query is None:
        return
    if message is None:
        kind = IDEA if (query.data or "").startswith(f"{IDEA}:") else "task"
        logger.warning("%s=lost: колбэк пришёл без доступного сообщения", kind)
        await query.answer()
        return
    if not permitted(message):
        return
    await query.answer()
    pressed = chosen_of(query)
    if pressed is None:
        await refuse(message, "stale_button", STALE_BUTTON, run=NO_RUN)
        return
    parent_id, number = pressed
    task = NO_RUN if number is None else number
    if number is None and not settings.trello_idea_board_id:
        await refuse(message, "idea_board_missing", IDEA_BOARD_MISSING, run=parent_id)
        return
    if running.locked():
        await refuse(message, "busy", BUSY, run=parent_id, task=task)
        return

    async with running:
        stopped = await asyncio.to_thread(waiting_for, message.chat_id)
        if stopped is not None:
            await refuse(
                message,
                "stop_alive",
                STOP_ALIVE.format(run_id=stopped.run_id),
                run=stopped.run_id,
                task=task,
            )
            return
        parent = await asyncio.to_thread(parent_of, parent_id, message.chat_id)
        # Идея берёт и разбор без поручений: своя идея часто его и даёт.
        if parent is None or (number is not None and parent.status != REVIEWED):
            await refuse(message, "stale_button", STALE_BUTTON, run=parent_id, task=task)
            return
        needed = (REVIEW_JSON,) if number is not None else (REVIEW_JSON, TRANSCRIPT)
        if not all((RUNS / parent_id / path).exists() for path in needed):
            await refuse(message, "parent_gone", PARENT_GONE, run=parent_id, task=task)
            return
        child = await asyncio.to_thread(child_of, parent_id, number)
        if child is not None and child.status == PUBLISHED:
            await refuse_published(message, child.run_id, parent_id, number)
            return
        if child is not None and child.status == QUESTIONS_SENT and number is not None:
            # Вопросы уже собраны и лежат в `clarify.json`: второе нажатие присылает их заново,
            # не зовя модель. Этим же лечится сообщение, не дошедшее до чата.
            logger.info("questions=resent run=%s chat=%s", child.run_id, message.chat_id)
            await send_questions(message, child.run_id, RUNS / child.run_id, number)
            return
        first = IDEA_ROUTE_START if number is None else TASK_ROUTE_START
        if child is None:
            run = child_run(new_run_id(), parent, number, auto_approve_for(message.chat_id))
            start, resume = first, NO_RUN
            await asyncio.to_thread(
                start_run,
                run.run_id,
                message.chat_id,
                run.source,
                run.lang,
                run.auto_approve,
                None,
                start,
                parent_id,
                number,
                # Ресёрч поручения выбирается кнопкой. Пока кнопки «Шаги без ресёрча» нет,
                # нажимают только обычную, а она значит «с ресёрчем».
                True if number is not None else None,
            )
        else:
            # Тот же run_id, а не новый: карточка сорванной публикации несёт его в маркере, и
            # только с ним повтор найдёт её на доске, а не поставит вторую рядом (§6).
            run = child_run(child.run_id, parent, number, child.auto_approve)
            start = resume = await asyncio.to_thread(resumed_start, run.root, first)
            await asyncio.to_thread(reopen_run, run.run_id, start)
        logger.info(
            "start=%s run=%s parent=%s task=%s chat=%s resume=%s",
            "task" if number is not None else IDEA,
            run.run_id,
            parent_id,
            task,
            message.chat_id,
            resume,
        )
        # Ответом на нажатое сообщение: под разбором поручений несколько, и ход прогона должен
        # называть, что из разбора пошло в работу. В личном чате PTB без do_quote не цитирует.
        note = await message.reply_text(
            progress_text(run, done_before(start), start), do_quote=True
        )
        # Вопросы для тимлида уходят ответом на само поручение, а не на сообщение о ходе прогона:
        # так первое нажатие и повторное отвечают в одно место.
        await follow(note, run, datetime.now(timezone.utc), start, answering=message)


async def refuse_published(
    message: Message, child_id: str, parent_id: str, number: int | None
) -> None:
    """Выбор из разбора уже на доске: ссылка вместо второго прогона."""
    if number is None:
        await refuse(
            message,
            "already_published",
            IDEA_ALREADY_PUBLISHED.format(run_id=child_id, url=idea_board_url()),
            run=child_id,
            parent=parent_id,
            task=NO_RUN,
        )
        return
    try:
        card = await asyncio.to_thread(task_card, RUNS / child_id)
    except OSError:
        # Каталог ребёнка убран руками, а разбор остался. Новый прогон тут неверен: он
        # поставил бы вторую карточку рядом с первой, а маркер её всё ещё найдёт.
        await refuse(
            message,
            "already_published",
            TASK_FILES_GONE.format(number=number, run_id=child_id),
            run=child_id,
            parent=parent_id,
            task=number,
            journal="gone",
        )
        return
    await refuse(
        message,
        "already_published",
        ALREADY_PUBLISHED.format(number=number, key=card.key, url=card.url),
        run=child_id,
        parent=parent_id,
        task=number,
    )


async def on_recording(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запись с диктофона: сначала вопрос о согласии, и больше ничего (SPEC §3.1).

    Замок здесь не проверяется: вопрос ничего не стоит, а прогон начинает нажатие, и замок
    проверит оно.
    """
    message = update.message
    if message is None or not permitted(message):
        return
    recording = message.audio or message.document
    # Фильтр обработчика `AUDIO | Document.AUDIO`: одно из двух есть всегда.
    assert recording is not None
    if recording.file_size is not None and recording.file_size > MAX_FILE_BYTES:
        await refuse(message, "too_big", FILE_TOO_BIG, bytes=recording.file_size)
        return
    if message.audio and reported_seconds(message.audio) > MAX_FILE_SECONDS:
        await refuse(
            message, "file_too_long", FILE_TOO_LONG, seconds=reported_seconds(message.audio)
        )
        return
    stopped = await asyncio.to_thread(waiting_for, message.chat_id)
    if stopped is not None:
        await refuse(
            message, "stop_alive", STOP_ALIVE.format(run_id=stopped.run_id), run=stopped.run_id
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
        await close_consent_question(query, message.chat_id, CONSENT_NO, NO_RUN)
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
        # Вопрос задан без остановки, но прогон, шедший в это время, мог встать на воротах.
        # Клавиатура остаётся: на вопрос ответят, когда остановка будет решена.
        stopped = await asyncio.to_thread(waiting_for, message.chat_id)
        if stopped is not None:
            await refuse(
                message,
                "stop_alive",
                STOP_ALIVE.format(run_id=stopped.run_id),
                run=stopped.run_id,
                press="yes",
                by=by,
            )
            return
        run_id = new_run_id()
        audio = RUNS / run_id / f"inputs/recording{Path(recording.file_name or '').suffix}"
        run = started_run(
            run_id, message.chat_id, source="file", audio=audio, consent_confirmed=True
        )
        await asyncio.to_thread(
            start_run,
            run_id,
            message.chat_id,
            run.source,
            run.lang,
            run.auto_approve,
            run.consent_confirmed,
            FIRST_STAGE,
        )
        logger.info("start=file run=%s chat=%s", run_id, message.chat_id)
        logger.info("consent=yes chat=%s by=%s run=%s", message.chat_id, by, run_id)
        await close_consent_question(query, message.chat_id, CONSENT_YES, run_id)
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
            # Как и длинная: расшифровывать её никто не будет, KEEP_AUDIO тут ни при чём.
            audio.unlink()
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
        await follow(note, run, datetime.now(timezone.utc))


async def close_consent_question(
    query: CallbackQuery, chat_id: int, pressed: str, run_id: str
) -> None:
    """Ответ дописывается в сам вопрос, кнопки уходят тем же вызовом.

    Вопрос с кнопками, оставшийся в чате, звал бы нажать «Да» ещё раз: та же запись скачалась бы
    и второй раз ушла бы в OpenAI. Сорванная правка прогон не держит, но молчать о ней нельзя.
    """
    answer = dict(CONSENT_BUTTONS)[pressed]
    at = datetime.now(timezone.utc).strftime(CONSENT_ANSWERED_AT)
    try:
        await query.edit_message_text(f"{CONSENT_QUESTION}\n\n{answer}. {at}")
    except TelegramError:
        logger.warning(
            "consent_question=kept chat=%s run=%s: ответ в вопрос не дописан, кнопки остались",
            chat_id,
            run_id,
        )


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
    answering: Message | None = None,
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
        written = progress_text(run, done, start)
        if stage.name == CLARIFY_STAGE and not parks_after_clarify(run):
            # Незаданные стандарты владелец обязан увидеть до того, как за них заплачено. Когда
            # вопросы уходят сообщением, строка стоит там; без них показать её больше негде.
            snapshot = read_project(read_artifact(run.root, PROJECT))
            written = f"{written}\n\n{standards_line(snapshot)}"
        progress.append(asyncio.run_coroutine_threadsafe(note.edit_text(written), loop))

    ending = await outcome(run, report, start, redo)
    try:
        if ending.parked:
            # Не остановка: места остановки в чате парковка не занимает, и статус ей пишет
            # `park_run`, а не род `Pause` (SPEC §3.2).
            await asyncio.to_thread(park_run, run.run_id)
        elif ending.stop:
            await asyncio.to_thread(
                stop_run,
                run.run_id,
                stopping(ending.stop),
                ending.stop.stage,
                ending.stop.artifact,
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
        if ending.stop.stage == STEPS_STAGE:
            # Текст для копирования отдельным сообщением и до файла: в нём ни подписей бота, ни
            # кнопок, чтобы владелец мог показать его тимлиду как есть.
            with suppress(TelegramError):
                await note.reply_text(
                    steps_copy_text(Steps.model_validate_json(read_artifact(run.root, STEPS_JSON)))
                )
        with suppress(TelegramError):
            await note.reply_document(run.root / ending.stop.artifact)
    if ending.parked and run.assignment is not None:
        await send_questions(answering or note, run.run_id, run.root, run.assignment)
    if ending.review is not None:
        await send_review(note, run, ending.review)
    # Сколько человек прождал ответа: отчёт серии живых прогонов (P2-06) отвечает на этот вопрос
    # числом, а из длительностей стадий его не сложить — между ними скачивание, публикация и
    # правки.
    waited = datetime.now(timezone.utc) - asked_at
    logger.info("run=%s seconds=%d", run.run_id, round(waited.total_seconds()))


async def send_review(note: Message, run: Run, review: Review) -> None:
    """Поручения сообщениями в порядке встречи, затем разбор и расшифровка файлами (SPEC §7.3).

    Строка уже закрыта, и каждая отправка прикрыта отдельно: сорванное сообщение одного
    поручения не должно отнять у человека остальные и расшифровку, по которой их проверяют.
    Расшифровка уходит и при пустом разборе: «заданий нет» проверяют как раз по ней.
    """
    for number, parts in enumerate(review_messages(review), start=1):
        for text in parts[:-1]:
            with suppress(TelegramError):
                await note.reply_text(text)
        # Кнопка на последней части: под ней поручение уже прочитано целиком.
        with suppress(TelegramError):
            await note.reply_text(parts[-1], reply_markup=task_keyboard(run.run_id, number))
    for document in (REVIEW_MD, TRANSCRIPT):
        with suppress(TelegramError):
            await note.reply_document(run.root / document)


def done_before(start: str) -> list[str]:
    """Стадии, пройденные до start: продолженный прогон не показывает их незаконченными."""
    first, _ = route_of(start)
    return list(NAMES[NAMES.index(first) : NAMES.index(start)])


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
    расходиться им нечем. `by` идёт в лог: кнопку затевали ради того, чтобы не набирать
    номер, и ответ на «пользовались ли ею» берётся из лога, а не из памяти.
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
    # «сколько человек выбрало» отчёт серии живых прогонов (P2-06) взять было неоткуда.
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


def sounds_like_approval(written: str) -> bool:
    """Согласие ли это целиком: «ok, aber Schritt 3 weglassen» согласием не считается.

    Пунктуация выбрасывается любая, не только ASCII: «Passt so!» и «ок.» это то же самое слово.
    """
    bare = "".join(
        " " if unicodedata.category(character).startswith("P") else character
        for character in written.casefold()
    )
    return " ".join(bare.split()) in APPROVALS


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


def idea_keyboard(run_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(IDEA_BUTTON, callback_data=f"{IDEA}:{run_id}")]]
    )


def questions_keyboard(run_id: str) -> InlineKeyboardMarkup:
    # Кнопка называет прогон, но не место: вопросы у прогона одни, второго круга не бывает.
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(title, callback_data=f"asked:{run_id}:{decision}")
                for decision, title in QUESTIONS_BUTTONS
            ]
        ]
    )


def task_keyboard(run_id: str, number: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(TASK_BUTTON, callback_data=f"task:{run_id}:{number}")]]
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


def steps_of(run: Run) -> str:
    """Шаги на воротах: числа из steps.json и стандарты, если снимок прогона уже не свежий."""
    steps = Steps.model_validate_json(read_artifact(run.root, STEPS_JSON))
    lines = [steps_digest(steps), f"Вопросов без ответа: {len(steps.unanswered)}."]
    try:
        current_standards()
    except ProjectContextError:
        # Каталог перестал читаться между вопросами и шагами: шаги написаны по снимку, и
        # владелец должен видеть, по какому, прежде чем подтвердить их кнопкой.
        lines.append(standards_snapshot_line(read_project(read_artifact(run.root, PROJECT))))
    return "\n".join(lines)


def stopping(pause: Pause) -> StopKind:
    """Род остановки, которым закрывают строку. Парковка сюда не доходит: её пишет `park_run`."""
    if pause.kind == "questions":
        raise ValueError(f"вопросы для тимлида не остановка: пауза после {pause.stage}")
    return pause.kind


def parks_after_clarify(run: Run) -> bool:
    """Прогон встанет на вопросах: есть кому их отбирать и есть что спрашивать (SPEC §3.2)."""
    return run.asks_teamlead and has_questions(
        Clarify.model_validate_json(read_artifact(run.root, CLARIFY_JSON))
    )


# Ворота объявляет список стадий (`gate_after`), а короткое содержание пишет код: третьи
# ворота, заведённые данными, обязаны упереться здесь, а не показать человеку «Бриф готов.»
# над чужим артефактом.
GATE_DIGESTS: dict[str, Callable[[Run], str]] = {
    "brief": lambda run: brief_digest(read_artifact(run.root, BRIEF)),
    "decompose": backlog_of,
    "steps": steps_of,
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


def task_of(query: CallbackQuery) -> tuple[str, int] | None:
    """Разбор и номер поручения из данных кнопки. Поручения нумеруются с единицы, как в разборе."""
    parts = button_of(query, 3)
    if not parts or not parts[2].isdecimal() or int(parts[2]) < 1:
        return None
    return parts[1], int(parts[2])


def chosen_of(query: CallbackQuery) -> tuple[str, int | None] | None:
    """Разбор и выбор из него: номер поручения у `task:`, None у `idea:`, то есть вся запись."""
    if (query.data or "").startswith(f"{IDEA}:"):
        parts = button_of(query, 2)
        return (parts[1], None) if parts else None
    return task_of(query)


def asked_of(query: CallbackQuery) -> tuple[str, str] | None:
    """Прогон и решение по вопросам для тимлида из данных кнопки."""
    parts = button_of(query, 3)
    return (parts[1], parts[2]) if parts and parts[2] in dict(QUESTIONS_BUTTONS) else None


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


def card_ending(run: Run) -> Ending:
    card = task_card(run.root)
    logger.info("run=%s finished card=%s", run.run_id, card.key)
    return Ending(text=CARD_FINISHED.format(key=card.key, url=card.url), status=PUBLISHED)


def review_ending(run: Run) -> Ending:
    """Разбор готов: оглавление встаёт в сообщение о ходе прогона, строка закрывается."""
    review = Review.model_validate_json(read_artifact(run.root, REVIEW_JSON))
    logger.info("run=%s reviewed tasks=%d", run.run_id, len(review.tasks))
    # Кнопка идеи под оглавлением и при пустом разборе: своя идея часто поручений не даёт.
    return Ending(
        text=review_lead(review),
        status=REVIEWED if review.tasks else NO_TASK,
        keyboard=idea_keyboard(run.run_id),
        review=review,
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
        waiting = await asyncio.to_thread(walk, run, start, route_end(start), report, redo)
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
        if waiting and waiting.kind == "questions":
            asked = Clarify.model_validate_json(read_artifact(run.root, CLARIFY_JSON))
            logger.info("stop=questions run=%s questions=%d", run.run_id, len(asked.questions))
            return Ending(text=QUESTIONS_PARKED.format(run_id=run.run_id), parked=True)
        if waiting:
            logger.info("stop=gate run=%s stage=%s", run.run_id, waiting.stage)
            return Ending(
                text=gate_text(run, waiting),
                stop=waiting,
                keyboard=gate_keyboard(run.run_id, waiting.stage),
            )
        if route_end(start) == "review":
            return review_ending(run)
        if route_end(start) == TASK_ROUTE_END:
            return card_ending(run)
        cards = cards_published(run.root)
        logger.info("run=%s finished cards=%d", run.run_id, cards)
        return Ending(text=finished_text(run.root), status=PUBLISHED)
    except NothingHeard as error:
        logger.warning("Прогон %s не услышал в записи ничего: %s", run.run_id, error)
        return Ending(text=str(error), status=NO_TASK)
    except TranscriptionError as error:
        # Текст такой ошибки написан человеку, а не в лог: показываем как есть. Статус `failed`,
        # а не `no_task`: сломанный ffmpeg — это поломка установки, и отчёт серии живых прогонов
        # не должен считать её записью без задания.
        logger.warning("Прогон %s не расшифровал запись: %s", run.run_id, error)
        return Ending(text=str(error), status=FAILED)
    except OSError:
        # Файлы прогона мог унести `make clean-runs` между прогонами. Повторять нечего:
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

    Молчание тут дороже сообщения: человек ждёт ответа на голосовое, которого больше
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
    # Режим прогона — в лог одной строкой: перед работой важно знать, сняты ли
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
    application.add_handler(CallbackQueryHandler(on_questions_button, pattern=r"^asked:"))
    application.add_handler(CallbackQueryHandler(on_consent_button, pattern=r"^consent:"))
    application.add_handler(
        CallbackQueryHandler(on_child_button, pattern=rf"^(task|{IDEA}):")
    )
    # Последним и почти без фильтра по типу: молчание в ответ на присланный файл или на опечатку
    # в команде человек читает как поломку бота. /start сюда не доходит, его забирает
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
    # прогон простоял между стадиями, а разбор живого прогона спрашивает именно это.
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("app").setLevel(logging.INFO)
    main()
