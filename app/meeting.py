"""Вход записи с ноутбука до согласия: цена, вопрос и копия записи. См. SPEC.md §3.1.

Общее место двух входов, терминального (`make meeting`) и папки входящих: текст, который
человек читает перед тем, как заплатить, обязан быть одним и тем же. Тело прогона у них разное
и живёт у каждого своё.
"""

import math
import shutil
from pathlib import Path

from app.config import settings
from app.render import minutes_count

# $0.006 за минуту записи: час стоит $0.36. Считается по целым минутам, как их и тарифицируют.
WHISPER_PER_MINUTE = 0.006

# Единственный замер длинного разбора: прогон 54f48ea8e0bc45c7, 19.09.2026. 44 минуты записи дали
# 22 643 токена на вход и 20 439 на выход, то есть $0.37 по тарифу Sonnet. Короткую встречу линия
# по одной точке завышает вдвое, и это выбранная сторона ошибки.
MEASURED_REVIEW_MINUTES = 44
MEASURED_REVIEW_DOLLARS = 0.37
MEASURED_REVIEW_OUTPUT_TOKENS = 20439

CONSENT_QUESTION = "Все, чьи голоса в записи есть, знали о записи и согласны на это?"

ONE_PASS_WARNING = (
    "У записи длиннее {minutes} минут разбор может не уместиться в один проход, а расшифровка к "
    "этому моменту уже оплачена. Поднимите ANTHROPIC_MAX_TOKENS в .env, если разбор оборвётся."
)


def whisper_price(seconds: int) -> str:
    return f"${math.ceil(seconds / 60) * WHISPER_PER_MINUTE:.2f}"


def review_price(seconds: int) -> str:
    dollars = math.ceil(seconds / 60) * MEASURED_REVIEW_DOLLARS / MEASURED_REVIEW_MINUTES
    return f"${dollars:.2f}"


def one_pass_minutes() -> int:
    """За сколько минут записи разбор упирается в потолок одного ответа модели.

    От настройки, а не числом в коде: `ANTHROPIC_MAX_TOKENS` поднимают ровно затем, чтобы длинная
    запись прошла одним проходом, и граница обязана ехать вместе с ней. До разбора по частям это
    единственное, чем длинная запись лечится.
    """
    fits = settings.anthropic_max_tokens / MEASURED_REVIEW_OUTPUT_TOKENS
    return round(MEASURED_REVIEW_MINUTES * fits)


def price_line(seconds: int) -> str:
    """Whisper точной цифрой, разбор — оценкой по длине от единственного замера.

    Завышенная цена перед согласием никого не обманывает, заниженная обманывает ровно там, где
    человек на неё опирается: прошлая строка обещала $0.02 за разбор и промахнулась в 18 раз.
    """
    minutes = math.ceil(seconds / 60)
    lines = [
        f"Расшифровка уйдёт в OpenAI, за пределы EU, и будет стоить {whisper_price(seconds)}.",
        f"Разбор сверх этого — около {review_price(seconds)}: пересчёт по единственному замеру, "
        f"{MEASURED_REVIEW_MINUTES} минуты дали ${MEASURED_REVIEW_DOLLARS:.2f}.",
    ]
    if minutes > one_pass_minutes():
        lines.append(ONE_PASS_WARNING.format(minutes=one_pass_minutes()))
    return "\n".join(lines)


def consent_question(name: str, seconds: int) -> str:
    """Один вопрос на два факта: цена и согласие решаются одним человеком в один момент.

    Два вопроса подряд учат тому, что второй — формальность. Имя файла, а не путь: путь к
    домашнему каталогу владельца в этом тексте не нужен никому.
    """
    heading = f"Запись: {name}, {minutes_count(math.ceil(seconds / 60))}."
    return "\n".join([heading, price_line(seconds), CONSENT_QUESTION])


def copied_recording(recording: Path, root: Path) -> Path:
    """Копия, а не оригинал: расшифровка при KEEP_AUDIO=false удаляет то, что ей дали.

    Запись владельца лежит у него на диске и остаётся нетронутой при любом исходе прогона.
    """
    target = root / f"inputs/recording{recording.suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(recording, target)
    return target
