"""ingest: вход прогона → inputs/transcript.md. Здесь и только здесь рождается run_id.

См. SPEC.md §3.1. Аудио и Telegram — фазы 2 и 3, пока сюда приходит только текст.
"""

import secrets

import frontmatter


def new_run_id() -> str:
    return secrets.token_hex(4)


def build_transcript(text: str, lang: str, run_id: str) -> str:
    header = f"run_id: {run_id}\nsource: text\nduration: null\nlang: {lang}"
    return f"---\n{header}\n---\n\n{text.strip()}\n"


def run_id_of(transcript: str) -> str | None:
    found = frontmatter.loads(transcript).metadata.get("run_id")
    return found if isinstance(found, str) and found else None
