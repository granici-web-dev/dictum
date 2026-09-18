"""Контракт outputs/review.json: разбор встречи со всеми поручениями. См. SPEC.md §5, §7.

Файл пишет модель, а показывает человеку бот, поэтому форма проверяется на выходе стадии, как
issues.json в app/validate.py. Дословные фрагменты сверяются с расшифровкой отдельно от проверки:
несошедшийся после ремонта фрагмент стадию не роняет, а получает пометку (P3-08).
"""

import json
import re
import unicodedata
from collections.abc import Iterator
from typing import Literal

import frontmatter
from pydantic import BaseModel, ConfigDict, Field, ValidationError

# Непустой текст, а не непустая строка: суть из одних пробелов дошла бы до человека пустым местом.
NOT_BLANK = r"\S"

MAX_QUOTES = 3
MAX_TOPICS = 3

# У текста язык не распознаётся (`lang` там DEFAULT_LANG), и проверять по нему перевод значило бы
# требовать его наугад. Для текста правило держит только промпт.
RECOGNISED_LANGUAGE_SOURCES = ("voice", "file")

ELLIPSIS = re.compile(r"…|\.\.\.")

TICKET_KEY = r"^[A-Z][A-Z0-9]+-\d+$"

# Код языка, как его называет Whisper: две строчные буквы. У текста язык называет модель, и код
# проверяет только форму: «немецкий» или «de-DE» стадиям шагов не язык.
MEETING_LANG = r"^[a-z]{2}$"


class Said(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    original: str
    # Вписывает код по итогу сверки, а не модель: её `true` у выдуманной цитаты ничего не стоит.
    in_transcript: bool | None = None


class Quote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    original: str
    translation: str | None = None
    in_transcript: bool | None = None


class AskBack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    # Готовая фраза тимлиду на языке встречи. Её сочиняет модель, в записи её нет, и сверять нечего.
    question: str
    translation: str | None = None


def absent(value: object) -> bool:
    return value is None or value == []


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    summary: str = Field(pattern=NOT_BLANK)
    assigned_by: str
    assignee: str
    status: Literal["decision", "thinking_aloud"]
    # Поля тикета Jira в файле только у поручения из тикета: поручение встречи тикета не имеет, и
    # разбор без него остаётся тем же файлом, что до P3-11, байт в байт.
    ticket_key: str | None = Field(default=None, pattern=TICKET_KEY, exclude_if=absent)
    ticket_url: str | None = Field(default=None, exclude_if=absent)
    deadline: Said | None = None
    constraints: list[Said] = Field(default_factory=list)
    # Критерии приёмки дословно из тикета. Не `constraints`: условие работы и признак готовности
    # на карточке и в шагах читаются по-разному.
    acceptance: list[Said] = Field(default_factory=list, exclude_if=absent)
    do_not: list[Said] = Field(default_factory=list)
    ask_back: list[AskBack] = Field(default_factory=list)
    quotes: list[Quote] = Field(min_length=1, max_length=MAX_QUOTES)


class Review(BaseModel):
    """Отсутствие поручений объявляет пустой `tasks`: без поля схема отвергла бы ответ раньше."""

    model_config = ConfigDict(extra="forbid")

    # Вписывает код после проверок, как run_id в issues.json: с этого момента язык разбора
    # объявляет артефакт, а не настройка, которая может смениться.
    owner_lang: str | None = None
    # Язык, на котором говорили или написан тикет. Его называет модель, потому что у текста язык
    # не распознавал никто, а `lang` прогона там DEFAULT_LANG. Необязательное: разборы до P3-11
    # его не носят, и читать их это не мешает.
    meeting_lang: str | None = Field(default=None, pattern=MEETING_LANG)
    tasks: list[Task]
    topics: list[str] = Field(default_factory=list, max_length=MAX_TOPICS)


def fragments(review: Review) -> Iterator[Said | Quote]:
    """Дословные фрагменты в порядке чтения: срок, ограничения, критерии, «не надо», цитаты."""
    for task in review.tasks:
        if task.deadline:
            yield task.deadline
        yield from task.constraints
        yield from task.acceptance
        yield from task.do_not
        yield from task.quotes


def normalized(text: str) -> str:
    """Текст без регистра, пунктуации и пробелов.

    Пробелы уходят все, а не схлопываются: распознавание пишет «Login Formular», модель
    «Login-Formular», и оба должны сойтись. Цена: короткий фрагмент может найтись на стыке двух
    слов, а это дешевле ложной пометки у каждого склеенного слова.
    """
    return "".join(
        character
        for character in text.casefold()
        if not character.isspace() and not unicodedata.category(character).startswith("P")
    )


def found_in(original: str, spoken: str) -> bool:
    """`spoken` уже нормализован. Части фрагмента с многоточием ищутся по порядку."""
    parts = [part for part in (normalized(piece) for piece in ELLIPSIS.split(original)) if part]
    if not parts:
        return False
    position = 0
    for part in parts:
        found = spoken.find(part, position)
        if found < 0:
            return False
        position = found + len(part)
    return True


def spoken_text(transcript: str) -> str:
    # Frontmatter не сказан на встрече: `file` или `true` из него не подтверждают ни одного слова.
    return normalized(frontmatter.loads(transcript).content)


def unmatched_originals(review: Review, transcript: str) -> list[str]:
    spoken = spoken_text(transcript)
    return [piece.original for piece in fragments(review) if not found_in(piece.original, spoken)]


def literally_in(identifier: str, text: str) -> bool:
    """Идентификатор тикета в тексте буква в букву и целиком: `ABC-12` не находится в `ABC-123`.

    Без нормализации, в отличие от фрагментов: у ключа и ссылки нет «почти того же» написания.
    """
    return re.search(rf"(?<![\w-]){re.escape(identifier)}(?![\w-])", text) is not None


def unmatched_tickets(review: Review, transcript: str) -> list[str]:
    """Ключи и ссылки тикетов, которых в тексте нет. Претензия первого ответа, как у фрагментов."""
    written = frontmatter.loads(transcript).content
    return [
        identifier
        for task in review.tasks
        for identifier in (task.ticket_key, task.ticket_url)
        if identifier is not None and not literally_in(identifier, written)
    ]


def missing_translations(review: Review) -> list[str]:
    missing = []
    for task_index, task in enumerate(review.tasks):
        pairs: list[tuple[str, str | None]] = [
            (f"quotes.{index}", quote.translation) for index, quote in enumerate(task.quotes)
        ] + [
            (f"ask_back.{index}", ask.translation) for index, ask in enumerate(task.ask_back)
        ]
        missing += [
            f"tasks.{task_index}.{place}.translation"
            for place, translation in pairs
            if not (translation and translation.strip())
        ]
    return missing


def check_review(review_json: str, transcript: str, owner_lang: str) -> list[str]:
    """Претензии к ответу стадии, кроме сверки фрагментов: та живёт в `unmatched_originals`."""
    try:
        parsed = json.loads(review_json)
    except json.JSONDecodeError as error:
        return [f"не разбирается как JSON: {error}"]
    try:
        review = Review.model_validate(parsed)
    except ValidationError as error:
        return [
            f"{'.'.join(str(part) for part in problem['loc'])}: {problem['msg']}"
            for problem in error.errors()
        ]

    problems = []
    if review.tasks and review.topics:
        problems.append("topics: темы пишутся только при пустом tasks, а поручения есть")
    written = frontmatter.loads(transcript).metadata
    lang, source = written.get("lang"), written.get("source")
    if source not in RECOGNISED_LANGUAGE_SOURCES and review.meeting_lang is None:
        # У записи язык называет Whisper, и поле там никто не читает. У текста его не называл
        # никто: без него шаги и вопросы пишутся на языке, который выберет модель (P3-11).
        problems.append(
            "meeting_lang: язык этого текста не распознавал никто, назови его сам двумя буквами"
        )
    if source in RECOGNISED_LANGUAGE_SOURCES and lang != owner_lang:
        problems += [
            f"{place}: перевод пуст, а запись на {lang}, язык владельца {owner_lang}"
            for place in missing_translations(review)
        ]
    return problems


def stamp_review(review_json: str, transcript: str, owner_lang: str) -> str:
    """Готовый разбор: язык владельца и итог сверки каждого фрагмента вписаны кодом.

    Всё, что прислала модель в этих полях, затирается: пометку ставит только сверка.
    """
    review = Review.model_validate_json(review_json)
    review.owner_lang = owner_lang
    spoken = spoken_text(transcript)
    for fragment in fragments(review):
        fragment.in_transcript = found_in(fragment.original, spoken)
    # Ключ и ссылка, которых в тексте нет, обнуляются, а не помечаются: придуманный идентификатор
    # хуже отсутствующего, а пометка «не найдено» у него ничего не спасает.
    written = frontmatter.loads(transcript).content
    for task in review.tasks:
        if task.ticket_key is not None and not literally_in(task.ticket_key, written):
            task.ticket_key = None
        if task.ticket_url is not None and not literally_in(task.ticket_url, written):
            task.ticket_url = None
    return review.model_dump_json(indent=2) + "\n"
