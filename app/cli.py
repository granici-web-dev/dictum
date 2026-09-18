"""make run-text: прогон текста через стадии без Telegram.

Разбор аргументов и коды возврата; сам обход — в app/run.py, общий с ботом. Артефакты пишутся
относительно текущей директории, файлы репозитория читаются от его корня. См. SPEC.md §7.1.
"""

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import anthropic
import frontmatter
from pydantic import ValidationError

from app.answers import Answers, answers_file
from app.config import ConfigError, settings
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
    produced_by,
    route_end,
    stage_named,
    stages_between,
)
from app.review import Review
from app.run import Run, missing_before, read_artifact, walk, write_artifact
from app.stages import StageError
from app.steps import Assignment, ReviewUnusable, assignment_of, meeting_lang_of

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
FROM_STAGES = ("review", "intake", "brief", "research", "prd", "decompose", "steps")


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


def task_run(parser: CommandLineParser, number: int) -> Run:
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
            }
        )
    except ValidationError:
        parser.error(f"в {TRANSCRIPT} нет lang или source: повторите разбор")
    if run.parent_run_id is None:
        parser.error(f"в {TRANSCRIPT} нет run_id: повторите разбор")
    # Номер вне разбора проверяется до обхода: иначе прогон упал бы стадией, а это ошибка запуска.
    try:
        review = Review.model_validate_json(read_artifact(Path("."), REVIEW_JSON))
        assignment_of(
            review,
            number,
            run.run_id,
            run.parent_run_id,
            meeting_lang_of(run.source, run.lang, review.meeting_lang),
        )
    except ValidationError as error:
        parser.error(f"{REVIEW_JSON} не проходит схему: {error}")
    except ReviewUnusable as error:
        parser.error(str(error))
    return run


def steps_run(parser: CommandLineParser, answers_path: str | None) -> Run:
    """Повтор шагов по лежащему assignment.json: run_id прогона поручения берётся оттуда.

    С `answers_path` ответ тимлида из файла ложится в inputs/answers.md как пришедший: так
    локальный прогон проходит ту ветку, которую в боте открывает reply на вопросы.
    """
    for needed in stage_named("steps").inputs:
        if not Path(needed).exists():
            parser.error(f"для --from steps нужен {needed}: его пишет --task N")
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
        "--answers",
        metavar="FILE",
        help="С --from steps: ответ тимлида из файла, как его прислали, и шаги заново.",
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

    if args.answers is not None and args.start != "steps":
        parser.error("--answers повторяет шаги с ответом тимлида и идёт только с --from steps")

    if args.task is not None:
        if args.start or args.gates or args.text or args.file:
            # Ворота после последней стадии локального обхода не срабатывают, а вход берётся
            # из разбора: и --gates, и --from, и текст здесь ничего бы не сделали.
            parser.error("--task берёт вход из outputs/review.json и идёт без --from и --gates")
        return start_pipeline(task_run(parser, args.task), "assignment")

    if args.start == "steps":
        if args.text or args.file:
            parser.error("--from берёт вход из outputs/, текст и --file с ним не нужны")
        return start_pipeline(steps_run(parser, args.answers), "steps")

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
