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

# Два замера разбора, и оценка идёт прямой через них обоих. Прогон 54f48ea8e0bc45c7, 19.09.2026:
# 2640 секунд записи дали 22 643 токена на вход и 20 439 на выход, то есть $0.37 по тарифу Sonnet.
# Прогон fdbfda84c895f6b7, 20.09.2026: 204 секунды дали 7483 и 8518, то есть $0.15. Вторая точка
# опровергла первую линию: у разбора есть постоянная часть — системный промпт и размышление
# модели, — и линия из нуля по одной точке короткую запись не завышала, а занижала втрое.
SHORT_REVIEW_SECONDS = 204
SHORT_REVIEW_DOLLARS = 0.15
LONG_REVIEW_SECONDS = 2640
LONG_REVIEW_DOLLARS = 0.37
LONG_REVIEW_OUTPUT_TOKENS = 20439

# Граница фазы 1 — один запрос Whisper: 25 МБ перекодированного mp3 при 24 кбит/с, то есть
# около 138 минут (SPEC §8). Дальше нужна нарезка, и до неё запись отвергается своим отказом:
# без него человек платит временем за перекодирование и получает чужую ошибку OpenAI по размеру.
ONE_WHISPER_REQUEST_SECONDS = 138 * 60

TOO_LONG_FOR_ONE_REQUEST = (
    "Запись {name}, {length}. Одним запросом расшифровки проходит около {limit} минут, и эта "
    "длиннее. Разрежьте её пополам и отдайте две части по отдельности."
)

CONSENT_QUESTION = "Все, чьи голоса в записи есть, знали о записи и согласны на это?"

ONE_PASS_WARNING = (
    "У записи длиннее {minutes} минут разбор может не уместиться в один проход, а расшифровка к "
    "этому моменту уже оплачена. Поднимите ANTHROPIC_MAX_TOKENS в .env, если разбор оборвётся."
)


def billed_minutes(seconds: int) -> int:
    """Целые минуты: так их тарифицирует Whisper, и по ним же посчитаны оба замера разбора."""
    return math.ceil(seconds / 60)


def whisper_price(seconds: int) -> str:
    return f"${billed_minutes(seconds) * WHISPER_PER_MINUTE:.2f}"


def review_price(seconds: int) -> str:
    """Прямая через оба замера: постоянная часть около $0.13 и около полцента за минуту записи."""
    per_minute = (LONG_REVIEW_DOLLARS - SHORT_REVIEW_DOLLARS) / (
        billed_minutes(LONG_REVIEW_SECONDS) - billed_minutes(SHORT_REVIEW_SECONDS)
    )
    fixed = LONG_REVIEW_DOLLARS - billed_minutes(LONG_REVIEW_SECONDS) * per_minute
    return f"${fixed + billed_minutes(seconds) * per_minute:.2f}"


def one_pass_minutes() -> int:
    """За сколько минут записи разбор упирается в потолок одного ответа модели.

    От настройки, а не числом в коде: `ANTHROPIC_MAX_TOKENS` поднимают ровно затем, чтобы длинная
    запись прошла одним проходом, и граница обязана ехать вместе с ней. До разбора по частям это
    единственное, чем длинная запись лечится.
    """
    fits = settings.anthropic_max_tokens / LONG_REVIEW_OUTPUT_TOKENS
    return round(billed_minutes(LONG_REVIEW_SECONDS) * fits)


def price_line(seconds: int) -> str:
    """Whisper точной цифрой, разбор — оценкой по длине от двух замеров.

    Заниженная цена обманывает ровно там, где человек на неё опирается: строка до первого замера
    обещала $0.02 за разбор и промахнулась в 18 раз, а линия по одному замеру обещала $0.03 за
    трёхминутную запись, которая стоила $0.15. Оба раза мимо было в дешёвую сторону.
    """
    lines = [
        f"Расшифровка уйдёт в OpenAI, за пределы EU, и будет стоить {whisper_price(seconds)}.",
        f"Разбор сверх этого — около {review_price(seconds)}: пересчёт по двум замерам, "
        f"{minutes_count(billed_minutes(SHORT_REVIEW_SECONDS))} дали ${SHORT_REVIEW_DOLLARS:.2f}, "
        f"{minutes_count(billed_minutes(LONG_REVIEW_SECONDS))} — ${LONG_REVIEW_DOLLARS:.2f}.",
    ]
    if billed_minutes(seconds) > one_pass_minutes():
        lines.append(ONE_PASS_WARNING.format(minutes=one_pass_minutes()))
    return "\n".join(lines)


def longer_than_one_whisper_request(seconds: int) -> bool:
    return seconds > ONE_WHISPER_REQUEST_SECONDS


def too_long_refusal(name: str, seconds: int) -> str:
    return TOO_LONG_FOR_ONE_REQUEST.format(
        name=name,
        length=minutes_count(billed_minutes(seconds)),
        limit=ONE_WHISPER_REQUEST_SECONDS // 60,
    )


def consent_question(name: str, seconds: int) -> str:
    """Один вопрос на два факта: цена и согласие решаются одним человеком в один момент.

    Два вопроса подряд учат тому, что второй — формальность. Имя файла, а не путь: путь к
    домашнему каталогу владельца в этом тексте не нужен никому.
    """
    heading = f"Запись: {name}, {minutes_count(billed_minutes(seconds))}."
    return "\n".join([heading, price_line(seconds), CONSENT_QUESTION])


def copied_recording(recording: Path, root: Path) -> Path:
    """Копия, а не оригинал: расшифровка при KEEP_AUDIO=false удаляет то, что ей дали.

    Запись владельца лежит у него на диске и остаётся нетронутой при любом исходе прогона.
    """
    target = root / f"inputs/recording{recording.suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(recording, target)
    return target
