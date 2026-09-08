"""ingest: вход прогона → inputs/transcript.md. Здесь и только здесь рождается run_id.

См. SPEC.md §3.1. Расшифровка записи живёт в app/transcribe.py, здесь — только frontmatter.
"""

import secrets
from typing import Literal

import frontmatter


# Восемь байт, а не четыре: доска у прогонов общая, и совпадение идентификаторов склеило бы
# карточки двух прогонов — второй счёл бы чужие своими и не создал бы собственные.
RUN_ID_BYTES = 8

# Файл с диктофона (source: file) и согласие на запись к нему — P3-02.
Source = Literal["text", "voice"]


def new_run_id() -> str:
    return secrets.token_hex(RUN_ID_BYTES)


def build_transcript(
    text: str, lang: str, run_id: str, source: Source, duration: int | None
) -> str:
    seconds = "null" if duration is None else str(duration)
    header = f"run_id: {run_id}\nsource: {source}\nduration: {seconds}\nlang: {lang}"
    return f"---\n{header}\n---\n\n{text.strip()}\n"


def run_id_of(transcript: str) -> str | None:
    found = frontmatter.loads(transcript).metadata.get("run_id")
    return found if isinstance(found, str) and found else None
