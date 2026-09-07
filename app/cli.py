"""make run-text: прогон текста через стадии без Telegram. См. SPEC.md §7.1."""

import argparse
import logging
import sys
from pathlib import Path
from typing import NoReturn

import anthropic
import frontmatter

from app.config import settings
from app.stages import ROOT, MissingApiKey, StageError, StageResult, run_stage

logger = logging.getLogger(__name__)

TRANSCRIPT = "inputs/transcript.md"
IDEA = "inputs/idea.md"
CANDIDATES = "outputs/candidates.md"
BRIEF = "outputs/brief.md"
RESEARCH = "outputs/research.md"
PRD = "outputs/prd.md"
PRD_TEMPLATE = "templates/prd_oneshot.md"
RESEARCH_SKIPPED = "Ресёрч не запускался: локальный прогон через make run-text.\n"

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
    logger.info("записан %s", path)


def read_input(text: str) -> tuple[str, str | None]:
    post = frontmatter.loads(text)
    lang = post.metadata.get("lang")
    return post.content, lang if isinstance(lang, str) else None


def build_transcript(text: str, lang: str) -> str:
    return f"---\nsource: text\nduration: null\nlang: {lang}\n---\n\n{text.strip()}\n"


def run_and_write(
    stage: str,
    inputs: dict[str, str],
    params: dict[str, str] | None = None,
) -> StageResult:
    try:
        result = run_stage(stage, inputs, params=params)
    except StageError as error:
        write_artifact(f"outputs/{stage}.raw.md", error.raw)
        raise
    for path, content in result.files.items():
        write_artifact(path, content)
    return result


def run_pipeline(text: str, lang: str) -> int:
    transcript = build_transcript(text, lang)
    write_artifact(TRANSCRIPT, transcript)

    intake = run_and_write("intake", {TRANSCRIPT: transcript})
    if CANDIDATES in intake.files:
        logger.info(
            "Идей несколько. Выберите одну в %s и запустите прогон с её текстом.", CANDIDATES
        )
        return EXIT_NEEDS_A_DECISION

    brief = run_and_write(
        "brief",
        {IDEA: intake.files[IDEA]},
        params={"mode": "batch", "interactive": "false", "lang": lang},
    )
    write_artifact(RESEARCH, RESEARCH_SKIPPED)
    prd = run_and_write(
        "prd",
        {
            BRIEF: brief.files[BRIEF],
            RESEARCH: RESEARCH_SKIPPED,
            PRD_TEMPLATE: (ROOT / PRD_TEMPLATE).read_text(encoding="utf-8"),
        },
    )
    run_and_write("decompose", {PRD: prd.files[PRD]})
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = CommandLineParser(
        prog="app.cli", description="Прогон идеи через стадии без Telegram."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("text", nargs="?", help="Текст идеи.")
    source.add_argument("--file", help="Файл с текстом идеи вместо аргумента.")
    parser.add_argument(
        "--lang", help="Язык артефактов. По умолчанию из frontmatter входа, иначе DEFAULT_LANG."
    )
    args = parser.parse_args(argv)

    try:
        text: str = Path(args.file).read_text(encoding="utf-8") if args.file else args.text
    except OSError as error:
        parser.error(f"не читается {args.file}: {error.strerror}")
    if not text.strip():
        parser.error('текст пустой: make run-text TEXT="…" или --file путь')

    body, lang_of_input = read_input(text)
    lang: str = args.lang or lang_of_input or settings.default_lang

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        return run_pipeline(body, lang)
    except (StageError, MissingApiKey, anthropic.APIError) as error:
        logger.error("%s", error)
        return EXIT_STAGE_FAILED


if __name__ == "__main__":
    sys.exit(main())
