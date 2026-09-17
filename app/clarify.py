"""Контракт outputs/clarify.json: вопросы для тимлида по одному поручению. См. SPEC.md §5, §7.

Вопросы читает владелец: отбирает нужные и отправляет тимлиду сам, каждый отдельно. Поэтому у
списка нет ни вводной фразы, ни обращения, ни подписи, а у вопроса нет ссылок на соседей. Языки,
номер прогона и то, нашлась ли форма обращения в сказанном, вписывает код после проверок.
"""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.review import NOT_BLANK, Review, found_in, fragments, normalized
from app.steps import Assignment, Pair

MAX_QUESTIONS = 7
MAX_QUESTION_CHARACTERS = 300

Address = Literal["neutral", "informal", "formal"]


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Язык встречи: вопрос уходит тимлиду как есть, отдельным сообщением или комментарием.
    text: str = Field(pattern=NOT_BLANK, max_length=MAX_QUESTION_CHARACTERS)
    translation: str | None = None
    # Язык владельца: что изменится в шагах от ответа. Без него отбирать вопросы нечем.
    why: str = Field(pattern=NOT_BLANK)


class Clarify(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str | None = None
    meeting_lang: str | None = None
    owner_lang: str | None = None
    title: Pair
    address: Address
    address_quote: str | None = None
    address_in_text: bool | None = None
    # Пустой список это ответ «спрашивать нечего», а не недоработка: прогон идёт дальше.
    questions: list[Question] = Field(max_length=MAX_QUESTIONS)


def schema_problems(clarify_json: str) -> list[str]:
    try:
        parsed = json.loads(clarify_json)
    except json.JSONDecodeError as error:
        return [f"не разбирается как JSON: {error}"]
    try:
        Clarify.model_validate(parsed)
    except ValidationError as error:
        return [
            f"{'.'.join(str(part) for part in problem['loc'])}: {problem['msg']}"
            for problem in error.errors()
        ]
    return []


def address_in_text(quote: str | None, assignment: Assignment) -> bool:
    """Форма обращения видна в сказанном: во фрагментах поручения, сверенных с расшифровкой.

    Расшифровки целиком у стадии нет, у неё только поручение, поэтому ищется там же, где модель
    могла эту форму увидеть.
    """
    if quote is None:
        return False
    heard = [
        normalized(piece.original)
        for piece in fragments(Review(tasks=[assignment.task]))
        if piece.in_transcript
    ]
    return any(found_in(quote, fragment) for fragment in heard)


def check_clarify(clarify_json: str, assignment: Assignment) -> list[str]:
    """Претензии к ответу стадии. Перевод проверяется, только если язык встречи известен."""
    problems = schema_problems(clarify_json)
    if problems:
        return problems
    clarify = Clarify.model_validate_json(clarify_json)
    meeting_lang, owner_lang = assignment.meeting_lang, assignment.owner_lang
    if meeting_lang is not None and meeting_lang != owner_lang:
        untranslated = [("title", clarify.title.translation)] + [
            (f"questions.{index}", question.translation)
            for index, question in enumerate(clarify.questions)
        ]
        problems += [
            f"{place}.translation: перевод пуст, а встреча на {meeting_lang}, язык владельца "
            f"{owner_lang}"
            for place, translation in untranslated
            if not (translation and translation.strip())
        ]
    seen: dict[str, int] = {}
    for index, question in enumerate(clarify.questions):
        key = " ".join(question.text.casefold().split())
        if key in seen:
            problems.append(
                f"questions.{index}: тот же вопрос, что questions.{seen[key]}: оставь один"
            )
        seen.setdefault(key, index)
    if clarify.address == "informal" and not address_in_text(clarify.address_quote, assignment):
        problems.append(
            "address_quote: неформальное обращение без дословного фрагмента поручения, где эта "
            "форма видна: скопируй фрагмент как есть или напиши вопросы нейтрально или формально"
        )
    return problems


def stamp_clarify(clarify_json: str, assignment: Assignment) -> str:
    """Готовые вопросы: номер прогона, языки и находка формы обращения вписаны кодом."""
    clarify = Clarify.model_validate_json(clarify_json)
    clarify.run_id = assignment.run_id
    clarify.meeting_lang = assignment.meeting_lang
    clarify.owner_lang = assignment.owner_lang
    clarify.address_in_text = address_in_text(clarify.address_quote, assignment)
    return clarify.model_dump_json(indent=2) + "\n"


def has_questions(clarify: Clarify) -> bool:
    return bool(clarify.questions)
