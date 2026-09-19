"""Расшифровка записи: ffmpeg → mp3 16 kHz mono → Whisper. См. SPEC.md §3.1.

Внешняя граница прогона: подпроцесс и чужой API. Frontmatter собирает app/ingest.py.
"""

import logging
import subprocess
import time
from pathlib import Path

import openai
from openai import DefaultHttpxClient
from pydantic import BaseModel

from app.config import LiveApiNotAllowed, MissingApiKey, settings

logger = logging.getLogger(__name__)

# verbose_json с языком и длительностью отдаёт только whisper-1: у gpt-4o-transcribe этого
# формата нет, а язык прогона брать больше неоткуда.
MODEL = "whisper-1"

# Таймаут запроса считается от длины записи, а не берётся константой: константа, подобранная под
# часовой созвон, превратила бы сорванное соединение на тридцатисекундном голосовом в час
# молчания, а с двумя повторами SDK — в три часа. База это сегодняшнее значение и пол для
# коротких входов; множитель взят запасом примерно вчетверо к замеренному на живых прогонах.
TIMEOUT_BASE_SECONDS = 120.0
TIMEOUT_PER_AUDIO_SECOND = 0.25

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

FFPROBE_MISSING = (
    "ffprobe не найден, а без него не узнать длину записи с диктофона. Он ставится вместе с "
    "ffmpeg: brew install ffmpeg на macOS, apt install ffmpeg на Debian."
)

NOTHING_HEARD = "В записи не разобрать речи. Наговорите ещё раз, поближе к микрофону."

# Хвост stderr, а не весь вывод: ffmpeg печатает баннер сборки на десяток строк, а причина
# отказа всегда в конце.
STDERR_TAIL = 300


class Transcription(BaseModel):
    text: str
    lang: str
    duration_seconds: int


class TranscriptionError(RuntimeError):
    """Сообщение пишется человеку: бот показывает его как есть, не пряча в лог."""


class NothingHeard(TranscriptionError):
    """Расшифровка удалась, а говорить было нечего: пустая запись, не поломка.

    Разные вещи и по строке в `runs`: сломанный ffmpeg — `failed`, тишина — `no_task`.
    """


def installed(tool: str) -> bool:
    # OSError, а не только FileNotFoundError: файл бывает на месте, но без права на запуск, и
    # тогда старт должен назвать причину, а не упасть трассировкой.
    try:
        subprocess.run([tool, "-version"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def recording_seconds(recording: Path) -> int:
    """Длительность записи, прочитанная из самого файла.

    Длительность у присланного аудио называет клиент Telegram, и она бывает неверной: лимит,
    поставленный по ней, пропускал бы длинную запись на расшифровку.
    """
    try:
        probed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(recording),
            ],
            capture_output=True,
            check=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise TranscriptionError(FFPROBE_MISSING) from error
    except subprocess.CalledProcessError as error:
        # ffprobe называет файл полным путём, а текст читает человек: путь на сервере не его дело.
        reason = error.stderr.replace(str(recording), recording.name).strip()[-STDERR_TAIL:]
        raise TranscriptionError(f"ffprobe не смог прочитать {recording.name}: {reason}") from error
    # У потока без длительности ffprobe печатает «N/A» и выходит с нулём.
    try:
        return round(float(probed.stdout.strip()))
    except ValueError as error:
        raise TranscriptionError(
            f"ffprobe не назвал длительность {recording.name}. Пересохраните запись "
            "в mp3, m4a или wav и пришлите ещё раз."
        ) from error


def recoded(source: Path) -> Path:
    # Не `with_suffix(".mp3")`: у присланного mp3 это тот же путь, что и вход, а ffmpeg править
    # файл на месте отказывается («Output same as Input») — прогон срывался на перекодировании.
    return source.with_name(f"{source.stem}.16k.mp3")


def convert_to_mp3(source: Path) -> Path:
    target = recoded(source)
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
        raise TranscriptionError(
            f"ffmpeg не смог перекодировать {source.name}: {error.stderr.strip()[-STDERR_TAIL:]}"
        ) from error
    return target


def http_client() -> DefaultHttpxClient:
    return DefaultHttpxClient()


def request_timeout(audio_seconds: int) -> float:
    return TIMEOUT_BASE_SECONDS + audio_seconds * TIMEOUT_PER_AUDIO_SECOND


def whisper_client(timeout: float) -> openai.OpenAI:
    """Свой клиент на прогон: таймаут у каждой записи свой, и кэшировать его нечем."""
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
        timeout=timeout,
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


def transcribe(audio: Path, run_id: str) -> Transcription:
    # Клиент строится до ffmpeg: он проверяет ALLOW_LIVE_API и ключ, и делать это после
    # перекодировки значит перемолоть часовую запись ради прогона, который всё равно откажется.
    # Длина меряется ещё раньше: без неё не выбрать таймаут, и стоит этот замер один быстрый
    # подпроцесс над заголовком файла.
    try:
        client = whisper_client(request_timeout(recording_seconds(audio)))
        mp3 = convert_to_mp3(audio)
        started = time.perf_counter()
        with mp3.open("rb") as recording:
            answer = client.audio.transcriptions.create(
                model=MODEL, file=recording, response_format="verbose_json"
            )
    finally:
        # При любом исходе, а не после успеха: запись с диктофона несёт чужие голоса, и сорванная
        # расшифровка не причина держать её у нас. Повтора с того же файла всё равно нет, бот
        # начинает новый прогон с нового скачивания.
        if not settings.keep_audio:
            audio.unlink(missing_ok=True)
            recoded(audio).unlink(missing_ok=True)
    logger.info(
        "stage=ingest run=%s model=%s audio_seconds=%d duration_ms=%d",
        run_id,
        MODEL,
        round(answer.duration),
        int((time.perf_counter() - started) * 1000),
    )
    text = answer.text.strip()
    if not text:
        raise NothingHeard(NOTHING_HEARD)
    return Transcription(
        text=text,
        lang=language_code(answer.language),
        duration_seconds=round(answer.duration),
    )
