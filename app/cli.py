"""Прогон без Telegram: `make run-text` по тексту и `make meeting` по записи с ноутбука.

Разбор аргументов и коды возврата; сам обход — в app/run.py, общий с ботом. `make run-text`
пишет артефакты относительно текущей директории, `make meeting` — в `runs/<run_id>/`, туда же,
куда пишет бот, и разбор он отдаёт в чат сам. См. SPEC.md §7.1.
"""

import argparse
import asyncio
import logging
import math
import shutil
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import anthropic
import frontmatter
from pydantic import ValidationError
from telegram import Bot
from telegram.error import TelegramError

from app.answers import Answers, answers_file
from app.bot import INGEST_LABEL, LABEL, RUNS, allowed_chats
from app.config import ConfigError, LiveApiNotAllowed, MissingApiKey, settings
from app.deliver import Delivery, idea_keyboard, send_tasks_and_documents
from app.ingest import new_run_id, run_id_of
from app.pipeline import (
    ANSWERS,
    ASSIGNMENT_JSON,
    NAMES,
    PUBLISHING,
    REVIEW_JSON,
    REVIEW_MD,
    STEPS_JSON,
    STEPS_MD,
    TRANSCRIPT,
    Stage,
    produced_by,
    route_end,
    stage_named,
    stages_between,
)
from app.render import minutes_count, review_lead
from app.review import Review
from app.run import Run, missing_before, read_artifact, walk, write_artifact
from app.stages import StageError
from app.steps import Assignment, ReviewUnusable, assignment_of
from app.store import NO_TASK, REVIEWED, ensure_schema, start_run
from app.transcribe import (
    FFMPEG_MISSING,
    FFPROBE_MISSING,
    TranscriptionError,
    installed,
    recording_seconds,
)

# Имя задано строкой, а не __name__: модуль запускают как `python -m`, и там __name__ — это
# "__main__", мимо дерева "app", которому в конце файла поднимают уровень до INFO. С __name__
# такие записи до человека не доходят.
logger = logging.getLogger("app.cli")

EXIT_OK = 0
EXIT_STAGE_FAILED = 1
EXIT_NEEDS_A_DECISION = 2
EXIT_USAGE = 64


class CommandLineParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def read_input(text: str) -> tuple[str, str | None]:
    post = frontmatter.loads(text)
    lang = post.metadata.get("lang")
    return post.content, lang if isinstance(lang, str) else None


# С чего локальный прогон может начать повтор: стадии модели и ресёрч, у которого есть свой файл.
# Стадии-коды, которые берут вход у разбора или пишут на доску, так не запускаются: у поручения
# для этого есть --task, у публикации своя команда.
FROM_STAGES = (
    "review",
    "intake",
    "brief",
    "research",
    "prd",
    "decompose",
    "approach",
    "steps",
)


def local_stop(start: str) -> str:
    """Где кончается локальный прогон: там же, где маршрут, только карточек он не публикует."""
    end = route_end(start)
    return NAMES[NAMES.index(end) - 1] if end in PUBLISHING else end


def start_pipeline(run: Run, start: str) -> int:
    stop = local_stop(start)
    try:
        waiting = walk(run, start, stop)
    except (StageError, ConfigError, anthropic.APIError) as error:
        logger.error("%s", error)
        return EXIT_STAGE_FAILED
    if waiting:
        if waiting.kind == "choice":
            logger.info(
                "Одной идеи не вышло: посмотрите %s и запустите прогон с текстом одной из них "
                "или с той же идеей подробнее.",
                waiting.artifact,
            )
        else:
            # Следующая стадия берётся из этого же прогона, а не из полного списка: у ворот на
            # последней стадии обхода её нет, и подсказка предложила бы publish, которого --from
            # не принимает.
            following = stages_between(waiting.stage, stop)[1]
            logger.info(
                "Ворота после %s: прочитайте %s и продолжите прогон с --from %s.",
                waiting.stage,
                waiting.artifact,
                following.name,
            )
        return EXIT_NEEDS_A_DECISION
    if stop == "review":
        logger.info("Разбор встречи готов: %s", REVIEW_MD)
    if stop == "steps":
        logger.info(
            "Шаги поручения готовы: %s. Опубликовать: python -m app.publish %s",
            STEPS_MD,
            STEPS_JSON,
        )
    return EXIT_OK


def task_run(parser: CommandLineParser, number: int, research: bool) -> Run:
    """Прогон поручения N из разбора в текущем каталоге: он же и каталог родителя."""
    for needed in (REVIEW_JSON, TRANSCRIPT):
        if not Path(needed).exists():
            parser.error(f"для --task нужен {needed}: сначала make run-text с текстом встречи")
    transcript = read_artifact(Path("."), TRANSCRIPT)
    written = frontmatter.loads(transcript).metadata
    # lang и source строки у локального прогона нет: их отпечаток лежит в расшифровке родителя.
    try:
        run = Run.model_validate(
            {
                "root": Path("."),
                "run_id": new_run_id(),
                "lang": written.get("lang"),
                "source": written.get("source"),
                "auto_approve": True,
                "parent_root": Path("."),
                "parent_run_id": run_id_of(transcript),
                "assignment": number,
                "skip": frozenset() if research else frozenset({"approach"}),
            }
        )
    except ValidationError:
        parser.error(f"в {TRANSCRIPT} нет lang или source: повторите разбор")
    if run.parent_run_id is None:
        parser.error(f"в {TRANSCRIPT} нет run_id: повторите разбор")
    # Номер вне разбора проверяется до обхода: иначе прогон упал бы стадией, а это ошибка запуска.
    try:
        review = Review.model_validate_json(read_artifact(Path("."), REVIEW_JSON))
        assignment_of(review, number, run.run_id, run.parent_run_id, run.source, run.lang)
    except ValidationError as error:
        parser.error(f"{REVIEW_JSON} не проходит схему: {error}")
    except ReviewUnusable as error:
        parser.error(str(error))
    return run


def steps_run(parser: CommandLineParser, start: str, answers_path: str | None) -> Run:
    """Повтор ресёрча или шагов по лежащим артефактам: run_id берётся из assignment.json.

    С `answers_path` ответ тимлида из файла ложится в inputs/answers.md как пришедший: так
    локальный прогон проходит ту ветку, которую в боте открывает reply на вопросы.
    """
    for needed in stage_named(start).inputs:
        if not Path(needed).exists():
            parser.error(f"для --from {start} нужен {needed}: его пишет --task N")
    if answers_path is not None:
        try:
            text = Path(answers_path).read_text(encoding="utf-8")
        except OSError as error:
            parser.error(f"не читается {answers_path}: {error.strerror}")
        if not text.strip():
            parser.error(f"{answers_path} пустой: ответа тимлида в нём нет")
        answers = Answers(status="answered", received_at=datetime.now(UTC), text=text)
        write_artifact(Path("."), ANSWERS, answers_file(answers))
    try:
        assignment = Assignment.model_validate_json(read_artifact(Path("."), ASSIGNMENT_JSON))
    except ValidationError as error:
        parser.error(f"{ASSIGNMENT_JSON} не проходит схему: {error}")
    # Язык и источник стадия шагов берёт из assignment.json, а не из прогона.
    return Run(
        root=Path("."),
        run_id=assignment.run_id,
        lang=settings.default_lang,
        source="text",
        auto_approve=True,
    )


# --- make meeting: запись с ноутбука до разбора в чате (P3-08, фаза 1) ---

FIRST_STAGE = NAMES[0]
REVIEW_STAGE = "review"
# Продолжение, которому осталась одна доставка: стадий у него нет вовсе.
DELIVERY_ONLY = "delivery"

# $0.006 за минуту записи: час стоит $0.36. Считается по целым минутам, как их и тарифицируют.
WHISPER_PER_MINUTE = 0.006

# Пустой экран на пять минут неотличим от зависшего процесса.
TICK_SECONDS = 30.0

CONSENT_WORD = "да"

CONSENT_ASKED = f"Введите «{CONSENT_WORD}», чтобы начать: "

CONSENT_QUESTION = "Все, чьи голоса в записи есть, знали о записи и согласны на это?"

NO_CONSENT = "Без согласия запись не расшифровывается."

# Без этого `echo да | make meeting …` превращает вопрос обратно во флаг, а согласие, которое
# ставится строкой в скрипте, — не согласие (CLAUDE.md §8).
NOT_A_TERMINAL = (
    "Согласие даёт человек за клавиатурой, а ввод идёт не с терминала. "
    "Запустите make meeting в терминале."
)

NO_RECORDING = "Файла {name} нет. Проверьте путь."

NOTHING_TO_RESUME = (
    "У прогона {run_id} нет ни расшифровки, ни разбора. Начните заново, с FILE=<путь к записи>."
)

SEVERAL_CHATS = (
    "Разрешённых чатов несколько, и в какой из них слать разбор — не угадать: соседний бывает "
    "групповым. Назовите его: make meeting FILE=… CHAT=<id>."
)

CHAT_NOT_A_NUMBER = "CHAT={asked} — это не id чата. У групп он отрицательный, у личного чата нет."

CHAT_NOT_ALLOWED = (
    "Чата {chat} нет в TELEGRAM_ALLOWED_CHAT_IDS. Впишите его туда или назовите другой."
)

DELIVERY_BROKEN = (
    "Разбор написан, а в чат не ушёл: {reason}. Прогон {run_id} цел, "
    "повторите доставку: make deliver RUN={run_id}"
)

DELIVERED = "Разбор ушёл в чат {chat}: сообщений {sent}."

NOT_DELIVERED = "Не ушло сообщений: {failed}. Повторить доставку: make deliver RUN={run_id}"


def whisper_price(seconds: int) -> str:
    return f"${math.ceil(seconds / 60) * WHISPER_PER_MINUTE:.2f}"


def price_line(seconds: int) -> str:
    """Whisper точной цифрой, разбор — названной неизвестностью.

    Придумать число за разбор значило бы нарушить правило 5 ровно там, где человек на это число
    опирается. После живого прогона фазы 1 замер появится, и строка станет диапазоном с числом.
    """
    return (
        f"Расшифровка уйдёт в OpenAI, за пределы EU, и будет стоить {whisper_price(seconds)}. "
        "Разбор сверх этого: на коротких встречах он стоил около $0.02, на часовой не замерен "
        "ни разу."
    )


# Продолжение платит только за то, за что ещё не платили: лежащая расшифровка снимает Whisper из
# сметы, лежащий разбор — и разбор тоже.
PRICE_WITHOUT_WHISPER = (
    "Расшифровка уже лежит, второй раз за неё не платим. Разбор: на коротких встречах он стоил "
    "около $0.02, на часовой не замерен ни разу."
)

PRICE_OF_NOTHING = "Платного в этом запуске нет: разбор написан, осталась доставка."


def spending(start: str, seconds: int) -> str:
    if start == DELIVERY_ONLY:
        return PRICE_OF_NOTHING
    if start == REVIEW_STAGE:
        return PRICE_WITHOUT_WHISPER
    return price_line(seconds)


def consent_question(recording: Path | None, seconds: int, start: str, run_id: str) -> str:
    """Один вопрос на два факта: цена и согласие решаются одним человеком в один момент.

    Два вопроса подряд учат тому, что второй — формальность. Имя файла, а не путь: путь к
    домашнему каталогу владельца в этом тексте не нужен никому.
    """
    heading = (
        f"Запись: {recording.name}, {minutes_count(math.ceil(seconds / 60))}."
        if recording is not None
        else f"Прогон {run_id}: запись уже расшифрована."
    )
    return "\n".join([heading, spending(start, seconds), CONSENT_QUESTION])


def consent_given(question: str) -> bool:
    """Согласие набирается словом на каждом прогоне (CLAUDE.md §8).

    Флага нет нарочно: он попадает в историю оболочки и повторяется стрелкой вверх вместе со всей
    командой, то есть даётся один раз в жизни. Набранное слово так не повторяется.
    """
    if not sys.stdin.isatty():
        raise ConfigError(NOT_A_TERMINAL)
    print(question)
    return input(CONSENT_ASKED).strip().casefold() == CONSENT_WORD


def armed_for_telegram() -> None:
    if not settings.allow_live_api:
        raise LiveApiNotAllowed(
            "ALLOW_LIVE_API не true, ничего не послано. Поставьте ALLOW_LIVE_API=true в .env "
            "для прогона, за который собираетесь заплатить."
        )
    if not settings.telegram_bot_token:
        raise MissingApiKey("TELEGRAM_BOT_TOKEN не задан, а разбор уходит в чат им.")


def armed_for_transcription() -> None:
    """Те же проверки, что бот делает на старте, и по той же причине (SPEC §7.3).

    Узнать о забытом ключе или о снесённом ffmpeg после $0.36 за Whisper — худший момент из
    возможных.
    """
    armed_for_telegram()
    if not settings.openai_api_key:
        raise MissingApiKey(
            "OPENAI_API_KEY не задан, а без него запись не расшифровать. "
            "Скопируйте .env.example в .env и заполните."
        )
    if not installed("ffmpeg"):
        raise ConfigError(FFMPEG_MISSING)
    if not installed("ffprobe"):
        raise ConfigError(FFPROBE_MISSING)


def chosen_chat(asked: str | None) -> int:
    """Чат доставки. Угадывать «первый по списку» нельзя: соседний бывает групповым."""
    allowed = allowed_chats()
    if asked is None:
        if len(allowed) != 1:
            raise ConfigError(SEVERAL_CHATS)
        return next(iter(allowed))
    if not asked.lstrip("-").isdigit():
        raise ConfigError(CHAT_NOT_A_NUMBER.format(asked=asked))
    chat_id = int(asked)
    if chat_id not in allowed:
        raise ConfigError(CHAT_NOT_ALLOWED.format(chat=chat_id))
    return chat_id


def resumed_meeting_start(root: Path) -> str:
    """Откуда вести продолженный прогон: по цепочке артефактов, что он успел записать.

    То же правило и тем же смыслом, что `resumed_start` у бота: не платить второй раз за то,
    что уже оплачено.
    """
    if (root / REVIEW_JSON).exists():
        return DELIVERY_ONLY
    if (root / TRANSCRIPT).exists():
        return REVIEW_STAGE
    return FIRST_STAGE


def meeting_recording(given: str | None, start: str, run_id: str) -> Path | None:
    """Запись, которую расшифруют. None — расшифровка уже лежит, и файл больше не нужен."""
    if start != FIRST_STAGE:
        return None
    if given is None:
        raise ConfigError(NOTHING_TO_RESUME.format(run_id=run_id))
    recording = Path(given)
    if not recording.is_file():
        raise ConfigError(NO_RECORDING.format(name=given))
    return recording


def copied_recording(recording: Path, root: Path) -> Path:
    """Копия, а не оригинал: расшифровка при KEEP_AUDIO=false удаляет то, что ей дали.

    Запись владельца лежит у него на диске и остаётся нетронутой при любом исходе прогона.
    """
    target = root / f"inputs/recording{recording.suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(recording, target)
    return target


@contextmanager
def ticking() -> Iterator[None]:
    """Строка с прошедшим временем каждые полминуты, пока идёт стадия."""
    started = time.monotonic()
    stop = threading.Event()

    def tick() -> None:
        while not stop.wait(TICK_SECONDS):
            print(f"  … идёт {round(time.monotonic() - started)} с")

    ticker = threading.Thread(target=tick, daemon=True)
    ticker.start()
    try:
        yield
    finally:
        stop.set()
        ticker.join()


def meeting_stage_done(stage: Stage) -> None:
    label = INGEST_LABEL["file"] if stage.name == FIRST_STAGE else LABEL[stage.name]
    print(f"✓ {label}")


def walked_meeting(run: Run, start: str) -> None:
    with ticking():
        walk(run, start, REVIEW_STAGE, meeting_stage_done)


async def delivered(chat_id: int, run_id: str, root: Path, review: Review) -> Delivery:
    """Разбор в чат: оглавление с кнопкой идеи, под ним поручения и файлы, как у бота.

    Сообщения шлёт сама команда, тем же токеном: polling для этого не нужен, и выключенный бот
    доставке не помеха. Кнопки под разбором сработают, когда его поднимут.
    """
    async with Bot(settings.telegram_bot_token) as messenger:
        lead = await messenger.send_message(
            chat_id, review_lead(review), reply_markup=idea_keyboard(run_id)
        )
        return await send_tasks_and_documents(lead, run_id, root, review)


def deliver_meeting(chat_id: int, run_id: str, root: Path) -> int:
    review = Review.model_validate_json(read_artifact(root, REVIEW_JSON))
    try:
        delivery = asyncio.run(delivered(chat_id, run_id, root, review))
    except TelegramError as error:
        print(DELIVERY_BROKEN.format(reason=error, run_id=run_id))
        return EXIT_STAGE_FAILED
    print(DELIVERED.format(chat=chat_id, sent=delivery.sent))
    if delivery.failed:
        print(NOT_DELIVERED.format(failed=delivery.failed, run_id=run_id))
    return EXIT_OK


def meeting_lang(root: Path, start: str) -> str:
    """Язык встречи: у продолженного прогона его назвал Whisper, и отпечаток лежит в расшифровке."""
    if start == FIRST_STAGE:
        return settings.default_lang
    _, spoken = read_input(read_artifact(root, TRANSCRIPT))
    return spoken or settings.default_lang


def meeting_run(given: str | None, asked_chat: str | None, resumed: str | None) -> int:
    """Запись с ноутбука до разбора в чате. Порядок проверок — из плана: отказ до оплаты."""
    run_id = resumed or new_run_id()
    root = RUNS / run_id
    try:
        armed_for_transcription()
        chat_id = chosen_chat(asked_chat)
        # Забытый `make db` иначе вылез бы после $0.36 за Whisper.
        ensure_schema()
        start = resumed_meeting_start(root) if resumed else FIRST_STAGE
        recording = meeting_recording(given, start, run_id)
        seconds = recording_seconds(recording) if recording is not None else 0
        agreed = consent_given(consent_question(recording, seconds, start, run_id))
    except (ConfigError, TranscriptionError) as error:
        print(error)
        return EXIT_USAGE
    if not agreed:
        print(NO_CONSENT)
        return EXIT_NEEDS_A_DECISION
    logger.info(
        "start=meeting run=%s chat=%s seconds=%d resume=%s",
        run_id,
        chat_id,
        seconds,
        start if resumed else "-",
    )
    logger.info("consent=yes run=%s chat=%s", run_id, chat_id)
    run = Run(
        root=root,
        run_id=run_id,
        lang=meeting_lang(root, start),
        audio=copied_recording(recording, root) if recording is not None else None,
        source="file",
        consent_confirmed=True,
        # Интерфейса решений у терминала нет, а ворот на маршруте записи и так не стоит.
        auto_approve=True,
    )
    if start != DELIVERY_ONLY:
        try:
            walked_meeting(run, start)
        except (StageError, ConfigError, TranscriptionError, anthropic.APIError) as error:
            logger.error("%s", error)
            return EXIT_STAGE_FAILED
    review = Review.model_validate_json(read_artifact(root, REVIEW_JSON))
    # Строка заводится один раз и сразу законченной: в рабочем статусе она не бывает ни секунды,
    # и уборка бота на старте (`fail_orphans`) не может счесть локальный прогон сорванным.
    start_run(
        run_id,
        chat_id,
        "file",
        run.lang,
        auto_approve=True,
        consent_confirmed=True,
        status=REVIEWED if review.tasks else NO_TASK,
    )
    logger.info("run=%s reviewed tasks=%d", run_id, len(review.tasks))
    return deliver_meeting(chat_id, run_id, root)


def deliver_only(run_id: str, asked_chat: str | None) -> int:
    """Повторная доставка готового разбора: ни базы, ни платного вызова тут нет."""
    root = RUNS / run_id
    try:
        armed_for_telegram()
        chat_id = chosen_chat(asked_chat)
        if not (root / REVIEW_JSON).exists():
            raise ConfigError(NOTHING_TO_RESUME.format(run_id=run_id))
    except ConfigError as error:
        print(error)
        return EXIT_USAGE
    logger.info("start=meeting run=%s chat=%s seconds=0 resume=deliver", run_id, chat_id)
    return deliver_meeting(chat_id, run_id, root)


def main(argv: list[str] | None = None) -> int:
    parser = CommandLineParser(
        prog="app.cli", description="Разбор встречи или прогон по стадиям без Telegram."
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("text", nargs="?", help="Текст встречи или поручения.")
    source.add_argument("--file", help="Файл с текстом вместо аргумента.")
    parser.add_argument(
        "--from",
        dest="start",
        choices=FROM_STAGES,
        help=(
            "Начать с этой стадии, взяв входные артефакты из outputs/. Без флага прогон "
            "кончается разбором; путь до decompose начинается с --from intake."
        ),
    )
    parser.add_argument(
        "--task",
        type=int,
        help="Разложить на шаги поручение N из лежащего outputs/review.json.",
    )
    parser.add_argument(
        "--no-research",
        dest="research",
        action="store_false",
        help="С --task: идти к шагам без ресёрча и без единого платного поиска.",
    )
    parser.add_argument(
        "--answers",
        metavar="FILE",
        help="С --from steps: ответ тимлида из файла, как его прислали, и шаги заново.",
    )
    parser.add_argument(
        "--meeting",
        metavar="FILE",
        help="Запись встречи с ноутбука: расшифровать, разобрать и отдать разбор в чат.",
    )
    parser.add_argument(
        "--chat", help="Чат доставки. При одном разрешённом чате подразумевается он."
    )
    parser.add_argument(
        "--run",
        help="Продолжить прогон с этим id по тому, что он успел записать: не платить дважды.",
    )
    parser.add_argument(
        "--deliver",
        metavar="RUN",
        help="Только отправить готовый разбор этого прогона ещё раз, ничего не считая.",
    )
    parser.add_argument(
        "--lang", help="Язык артефактов. По умолчанию из frontmatter входа, иначе DEFAULT_LANG."
    )
    parser.add_argument(
        "--gates",
        action="store_true",
        help="Останавливаться на воротах: без флага локальный прогон авто-подтверждён.",
    )
    args = parser.parse_args(argv)

    if args.deliver is not None:
        if args.meeting or args.run or args.start or args.task or args.text or args.file:
            parser.error("--deliver только отправляет лежащий разбор и идёт один, с --chat")
        return deliver_only(args.deliver, args.chat)

    if args.meeting or args.run:
        if args.start or args.task or args.text or args.file:
            parser.error("--meeting ведёт запись до разбора и идёт без --from, --task и текста")
        return meeting_run(args.meeting, args.chat, args.run)

    if args.chat is not None:
        parser.error("--chat называет чат доставки и значит что-то только с --meeting")

    if args.answers is not None and args.start != "steps":
        parser.error("--answers повторяет шаги с ответом тимлида и идёт только с --from steps")

    if not args.research and args.task is None:
        parser.error("--no-research выбирает режим для --task N и без него ничего не значит")

    if args.task is not None:
        if args.start or args.gates or args.text or args.file:
            # Ворота после последней стадии локального обхода не срабатывают, а вход берётся
            # из разбора: и --gates, и --from, и текст здесь ничего бы не сделали.
            parser.error("--task берёт вход из outputs/review.json и идёт без --from и --gates")
        return start_pipeline(task_run(parser, args.task, args.research), "assignment")

    if args.start in ("approach", "steps"):
        if args.text or args.file:
            parser.error("--from берёт вход из outputs/, текст и --file с ним не нужны")
        return start_pipeline(steps_run(parser, args.start, args.answers), args.start)

    if args.start:
        if args.text or args.file:
            parser.error("--from берёт вход из outputs/, текст и --file с ним не нужны")
        if not Path(TRANSCRIPT).exists():
            parser.error(f"для --from {args.start} нужен {TRANSCRIPT}: в нём run_id прогона")
        for needed in missing_before(Path('.'), args.start, local_stop(args.start)):
            # Пропущенная стадия свой артефакт не пишет, поэтому вместо «нет файла» полезнее
            # сказать, какая стадия его делает: обычно ответ — начать прогон на шаг раньше.
            maker = produced_by(needed)
            hint = f"; его делает {maker}, начните с --from {maker}" if maker else ""
            parser.error(f"для --from {args.start} нужен {needed}, а его нет{hint}")
        # Прогон продолжается, а не начинается, поэтому свой run_id ему брать неоткуда: выдать
        # второй значило бы, что у одного прогона их два, и publish создал бы карточки заново.
        transcript = read_artifact(Path("."), TRANSCRIPT)
        started = run_id_of(transcript)
        if not started:
            parser.error(
                f"в {TRANSCRIPT} нет run_id: транскрипт старше этого правила, начните прогон заново"
            )
        # Язык оттуда же, откуда run_id: с P3-01 транскрипт голосового несёт язык от Whisper, и
        # подстановка DEFAULT_LANG собрала бы немецкий бриф по русской идее.
        _, spoken = read_input(transcript)
        run = Run(
            root=Path("."),
            run_id=started,
            lang=args.lang or spoken or settings.default_lang,
            source="text",
            auto_approve=not args.gates,
        )
        return start_pipeline(run, args.start)

    if not (args.text or args.file):
        parser.error('нужен текст: make run-text TEXT="…", --file путь или --from стадия')
    try:
        text: str = Path(args.file).read_text(encoding="utf-8") if args.file else args.text
    except OSError as error:
        parser.error(f"не читается {args.file}: {error.strerror}")
    if not text.strip():
        parser.error('текст пустой: make run-text TEXT="…" или --file путь')

    body, lang_of_input = read_input(text)
    lang: str = args.lang or lang_of_input or settings.default_lang

    run = Run(
        root=Path("."),
        run_id=new_run_id(),
        lang=lang,
        text=body,
        source="text",
        auto_approve=not args.gates,
    )
    return start_pipeline(run, NAMES[0])


if __name__ == "__main__":
    logging.basicConfig(format="%(levelname)s %(message)s")
    logging.getLogger("app").setLevel(logging.INFO)
    sys.exit(main())
