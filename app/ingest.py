"""ingest: вход прогона → inputs/transcript.md. Здесь и только здесь рождается run_id.

См. SPEC.md §3.1. Расшифровка записи живёт в app/transcribe.py, здесь — только frontmatter.
"""

import re
import secrets
from typing import Literal

import frontmatter


# Восемь байт, а не четыре: доска у прогонов общая, и совпадение идентификаторов склеило бы
# карточки двух прогонов — второй счёл бы чужие своими и не создал бы собственные.
RUN_ID_BYTES = 8

Source = Literal["text", "voice", "file"]

RUN_ID_LINE = re.compile(r"^run_id:.*$", re.MULTILINE)


def new_run_id() -> str:
    return secrets.token_hex(RUN_ID_BYTES)


def build_transcript(
    text: str,
    lang: str,
    run_id: str,
    source: Source,
    duration: int | None,
    consent_confirmed: bool | None,
) -> str:
    seconds = "null" if duration is None else str(duration)
    # run_id в кавычках: из шестнадцати шестнадцатеричных знаков примерно один идентификатор
    # из тысячи восьмисот состоит из одних цифр, и YAML читает такой как число. `run_id_of`
    # тогда не находит строки, и `--from` отказывается продолжать прогон, у которого id есть.
    header = f'run_id: "{run_id}"\nsource: {source}\nduration: {seconds}\nlang: {lang}'
    # Строки нет вовсе, а не `null`: у текста и голосового вопроса о согласии не было (SPEC §3.1).
    if consent_confirmed is not None:
        header += f"\nconsent_confirmed: {str(consent_confirmed).lower()}"
    return f"---\n{header}\n---\n\n{text.strip()}\n"


def child_transcript(parent: str, run_id: str, parent_run_id: str) -> str:
    """Расшифровка разбора для дочернего прогона: номер свой, отпечаток записи родительский.

    `source`, `duration`, `lang` и согласие остаются как были: запись одна, и ребёнок её не
    получал. Строки правятся на месте, а не собираются заново из разобранного YAML: иначе
    run_id из одних цифр снова ушёл бы без кавычек.
    """
    if run_id_of(parent) != parent_run_id:
        raise ValueError(f"Расшифровка не прогона {parent_run_id}: отдать её ребёнку нельзя")
    header_end = parent.index("\n---\n", len("---\n"))
    header = RUN_ID_LINE.sub(
        f'run_id: "{run_id}"\nparent_run_id: "{parent_run_id}"', parent[:header_end], count=1
    )
    return header + parent[header_end:]


def run_id_of(transcript: str) -> str | None:
    found = frontmatter.loads(transcript).metadata.get("run_id")
    return found if isinstance(found, str) and found else None


def lang_of(transcript: str) -> str | None:
    """Язык записи из frontmatter: его назвал Whisper, и продолженный прогон берёт его отсюда."""
    found = frontmatter.loads(transcript).metadata.get("lang")
    return found if isinstance(found, str) and found else None
