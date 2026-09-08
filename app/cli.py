"""make run-text: прогон текста через стадии без Telegram.

Артефакты пишутся относительно текущей директории, файлы репозитория читаются от его корня.
См. SPEC.md §7.1.
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import NoReturn

import anthropic
import frontmatter

from app.config import ConfigError, settings
from app.ingest import build_transcript, new_run_id, run_id_of
from app.pipeline import CANDIDATES, NAMES, TRANSCRIPT, produced_by, stages_from
from app.stages import StageError, StageResult, load_template, run_stage

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_STAGE_FAILED = 1
EXIT_NEEDS_A_DECISION = 2
EXIT_USAGE = 64


class CommandLineParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def write_artifact(path: str, content: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    logger.info("Записан %s", path)


def read_input(text: str) -> tuple[str, str | None]:
    post = frontmatter.loads(text)
    lang = post.metadata.get("lang")
    return post.content, lang if isinstance(lang, str) else None


def run_and_write(
    stage: str,
    inputs: dict[str, str],
    params: dict[str, str],
    run_id: str,
) -> StageResult:
    try:
        result = run_stage(stage, inputs, run_id, params=params)
    except StageError as error:
        write_artifact(f"outputs/{stage}.raw.md", error.raw)
        raise
    for path, content in result.files.items():
        write_artifact(path, content)
    return result


def read_artifact(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def missing_before(start: str) -> list[str]:
    """Артефакты, которых обход не создаст сам, а прочитать попробует.

    Проверять только входы стартовой стадии мало: следующие читают то, что пропущенные должны
    были положить, и прогон падал бы посреди работы, успев записать часть файлов.
    """
    written: set[str] = set()
    missing = []
    for stage in stages_from(start):
        missing += [
            path for path in stage.inputs if path not in written and not Path(path).exists()
        ]
        # Пересечение, а не объединение: если у стадии несколько наборов выходов, гарантирован
        # лишь тот файл, который есть в каждом. intake отдаёт либо idea.md, либо candidates.md.
        written |= set.intersection(*(set(paths) for paths in stage.outputs))
    return missing


def run_pipeline(start: str, text: str, lang: str, run_id: str) -> int:
    if start == NAMES[0]:
        write_artifact(TRANSCRIPT, build_transcript(text, lang, run_id))

    for stage in stages_from(start):
        if stage.runs == "code":
            (written,) = stage.outputs[0]
            # Настоящий ресёрч, положенный руками или прошлым прогоном, затирать нечем.
            if not Path(written).exists():
                write_artifact(written, stage.content)
            continue

        inputs = {path: read_artifact(path) for path in stage.inputs}
        if stage.template:
            inputs[f"templates/{stage.template}"] = load_template(stage.template)
        params = dict(stage.params)
        if stage.needs_lang:
            params["lang"] = lang

        result = run_and_write(stage.name, inputs, params, run_id)
        # candidates.md объявляет в выходах только intake, поэтому спрашивать про стадию
        # отдельно незачем. Кандидаты — решение человека, дальше прогон не идёт.
        if CANDIDATES in result.files:
            logger.info(
                "Идей несколько. Выберите одну в %s и запустите прогон с её текстом.", CANDIDATES
            )
            return EXIT_NEEDS_A_DECISION
    return EXIT_OK


def start_pipeline(start: str, text: str, lang: str, run_id: str) -> int:
    try:
        return run_pipeline(start, text, lang, run_id)
    except (StageError, ConfigError, anthropic.APIError) as error:
        logger.error("%s", error)
        return EXIT_STAGE_FAILED


def main(argv: list[str] | None = None) -> int:
    parser = CommandLineParser(
        prog="app.cli", description="Прогон идеи через стадии без Telegram."
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("text", nargs="?", help="Текст идеи.")
    source.add_argument("--file", help="Файл с текстом идеи вместо аргумента.")
    parser.add_argument(
        "--from",
        dest="start",
        choices=NAMES[1:],
        help="Начать с этой стадии, взяв входные артефакты из outputs/.",
    )
    parser.add_argument(
        "--lang", help="Язык артефактов. По умолчанию из frontmatter входа, иначе DEFAULT_LANG."
    )
    args = parser.parse_args(argv)

    if args.start:
        if args.text or args.file:
            parser.error("--from берёт вход из outputs/, текст и --file с ним не нужны")
        if not Path(TRANSCRIPT).exists():
            parser.error(f"для --from {args.start} нужен {TRANSCRIPT}: в нём run_id прогона")
        for needed in missing_before(args.start):
            # Пропущенная стадия свой артефакт не пишет, поэтому вместо «нет файла» полезнее
            # сказать, какая стадия его делает: обычно ответ — начать прогон на шаг раньше.
            maker = produced_by(needed)
            hint = f"; его делает {maker}, начните с --from {maker}" if maker else ""
            parser.error(f"для --from {args.start} нужен {needed}, а его нет{hint}")
        # Прогон продолжается, а не начинается, поэтому свой run_id ему брать неоткуда: выдать
        # второй значило бы, что у одного прогона их два, и publish создал бы карточки заново.
        started = run_id_of(read_artifact(TRANSCRIPT))
        if not started:
            parser.error(
                f"в {TRANSCRIPT} нет run_id: транскрипт старше этого правила, начните прогон заново"
            )
        return start_pipeline(args.start, "", args.lang or settings.default_lang, started)

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

    return start_pipeline(NAMES[0], body, lang, new_run_id())


if __name__ == "__main__":
    logging.basicConfig(format="%(levelname)s %(message)s")
    logging.getLogger("app").setLevel(logging.INFO)
    sys.exit(main())
