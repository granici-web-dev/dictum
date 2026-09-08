"""Расшифровка записи: ffmpeg → mp3 16 kHz mono → Whisper. См. SPEC.md §3.1.

Внешняя граница прогона: подпроцесс и чужой API. Frontmatter собирает app/ingest.py.
"""

import logging
import subprocess
import time
from functools import cache
from pathlib import Path

import openai
from openai import DefaultHttpxClient
from pydantic import BaseModel

from app.config import LiveApiNotAllowed, MissingApiKey, settings

logger = logging.getLogger(__name__)

# verbose_json с языком и длительностью отдаёт только whisper-1: у gpt-4o-transcribe этого
# формата нет, а язык прогона брать больше неоткуда.
MODEL = "whisper-1"
REQUEST_TIMEOUT_SECONDS = 120.0

# Whisper называет язык английским словом («russian»), а артефакты прогона несут код. Таблица
# покрывает языки команды; незнакомое имя идёт дальше как есть — соврать про язык хуже.
LANGUAGE_CODES = {
    "english": "en",
    "german": "de",
    "romanian": "ro",
    "russian": "ru",
    "ukrainian": "uk",
}

FFMPEG_MISSING = (
    "ffmpeg не найден, а без него запись не перекодировать. Поставьте его: "
    "brew install ffmpeg на macOS, apt install ffmpeg на Debian."
)

NOTHING_HEARD = "В записи не разобрать речи. Наговорите ещё раз, поближе к микрофону."


class Transcription(BaseModel):
    text: str
    lang: str
    duration_seconds: int


class TranscriptionError(RuntimeError):
    """Сообщение пишется человеку: бот показывает его как есть, не пряча в лог."""


def ffmpeg_installed() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False
    return True


def convert_to_mp3(source: Path) -> Path:
    target = source.with_suffix(".mp3")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(source), "-ac", "1", "-ar", "16000", str(target)],
            capture_output=True,
            check=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise TranscriptionError(FFMPEG_MISSING) from error
    except subprocess.CalledProcessError as error:
        # Хвост, а не весь вывод: ffmpeg печатает баннер сборки на десяток строк, и причина
        # отказа всегда в конце.
        raise TranscriptionError(
            f"ffmpeg не смог перекодировать {source.name}: {error.stderr.strip()[-300:]}"
        ) from error
    return target


def http_client() -> DefaultHttpxClient:
    return DefaultHttpxClient()


@cache
def whisper_client() -> openai.OpenAI:
    if not settings.allow_live_api:
        raise LiveApiNotAllowed(
            "ALLOW_LIVE_API is not true, nothing was sent. "
            "Set ALLOW_LIVE_API=true in .env for a run you mean to pay for."
        )
    if not settings.openai_api_key:
        raise MissingApiKey("OPENAI_API_KEY is not set. Copy .env.example to .env and fill it in.")
    return openai.OpenAI(
        api_key=settings.openai_api_key,
        max_retries=2,
        timeout=REQUEST_TIMEOUT_SECONDS,
        http_client=http_client(),
    )


def language_code(detected: str) -> str:
    code = LANGUAGE_CODES.get(detected.lower())
    if code is None:
        logger.warning(
            "Whisper распознал язык «%s», кода для него нет: пишу имя как есть", detected
        )
        return detected
    return code


def transcribe(audio: Path) -> Transcription:
    mp3 = convert_to_mp3(audio)
    started = time.perf_counter()
    with mp3.open("rb") as recording:
        answer = whisper_client().audio.transcriptions.create(
            model=MODEL, file=recording, response_format="verbose_json"
        )
    logger.info(
        "stage=ingest model=%s audio_seconds=%d duration_ms=%d",
        MODEL,
        round(answer.duration),
        int((time.perf_counter() - started) * 1000),
    )
    text = answer.text.strip()
    if not text:
        raise TranscriptionError(NOTHING_HEARD)
    if not settings.keep_audio:
        audio.unlink()
        mp3.unlink()
    return Transcription(
        text=text,
        lang=language_code(answer.language),
        duration_seconds=round(answer.duration),
    )
