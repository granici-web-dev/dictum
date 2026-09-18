"""Шаги одного поручения: inputs/assignment.json и outputs/steps.json. См. SPEC.md §5, §7.

Вход собирает код из разбора родителя, выход пишет модель, а всё дословное из разбора, языки и
номер прогона вписывает код после проверок, тем же приёмом, что `owner_lang` в review.json.
Шаг это пункт чек-листа внутри одной карточки, поэтому ни оценки, ни метки, ни зависимостей у
него нет: повесить их на пункт чек-листа в Trello нельзя, а заполненные для порядка поля были бы
выдуманными данными.
"""

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.ingest import Source
from app.review import (
    NOT_BLANK,
    RECOGNISED_LANGUAGE_SOURCES,
    TICKET_KEY,
    AskBack,
    Review,
    Said,
    Task,
    absent,
)

MAX_STEPS = 10

# Пометка неясного места. Приставка не переводится, как английские заголовки полей; номер вопроса
# стоит после слова на языке своего текста: «[уточнить: Frage 2]», «[уточнить: вопрос 2]».
MARK = re.compile(r"\[уточнить:([^\]]*)\]")
MARK_NUMBER = re.compile(r"^(?:\S+\s+)?(\d+)$")


class ReviewUnusable(ValueError):
    """Из разбора не собрать вход стадии: номера нет в разборе или язык владельца не записан."""


class Pair(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Язык встречи.
    text: str = Field(pattern=NOT_BLANK)
    # Язык владельца; null, если text уже на нём.
    translation: str | None = None


class TaskRef(BaseModel):
    """Сказанное на встрече о поручении: переносит код из разбора, модель его не пишет."""

    model_config = ConfigDict(extra="forbid")

    parent_run_id: str
    number: int = Field(ge=1)
    ticket_key: str | None = Field(default=None, pattern=TICKET_KEY, exclude_if=absent)
    ticket_url: str | None = Field(default=None, exclude_if=absent)
    deadline: Said | None = None
    constraints: list[Said] = Field(default_factory=list)
    acceptance: list[Said] = Field(default_factory=list, exclude_if=absent)
    do_not: list[Said] = Field(default_factory=list)
    ask_back: list[AskBack] = Field(default_factory=list)


class AskedQuestion(BaseModel):
    """Вопрос для тимлида под своим номером: текст переносит код, модель его не переписывает."""

    model_config = ConfigDict(extra="forbid")

    number: int = Field(ge=1)
    text: str
    translation: str | None = None
    # Откуда вопрос: спросила стадия вопросов или открыл ресёрч. Нумерация общая и сквозная —
    # владелец отбирает их одним списком, и второго счёта у него в голове быть не должно.
    origin: Literal["clarify", "research"]
    answered: bool


class Steps(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str | None = None
    owner_lang: str | None = None
    meeting_lang: str | None = None
    task: TaskRef | None = None
    title: Pair
    summary: Pair
    steps: list[Pair] = Field(min_length=1, max_length=MAX_STEPS)
    open_questions: list[Pair] = Field(default_factory=list)
    # Номера вопросов без ответа, которые ещё нужны. Неотправленный вопрос и вопрос без ответа
    # это один случай: что из вопросов владелец отправил, не знает никто, кроме него.
    unanswered: list[int] = Field(default_factory=list)
    # Подход, по которому написаны шаги, одной строкой. Пока ресёрча нет, писать его не из чего.
    approach: Pair | None = None
    questions: list[AskedQuestion] = Field(default_factory=list)


class Assignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    parent_run_id: str
    number: int = Field(ge=1)
    # Язык встречи: у голосового и записи его назвало распознавание, у текста назвал разбор. Null
    # остаётся только у разбора до P3-11, который языка не называл: DEFAULT_LANG вместо него был бы
    # выдуманным языком встречи.
    meeting_lang: str | None
    owner_lang: str
    task: Task


def meeting_lang_of(source: Source, lang: str, named_by_review: str | None) -> str | None:
    """Язык встречи: у голосового и записи его назвал Whisper, у текста — разбор.

    `lang` прогона у текста это DEFAULT_LANG, а не язык, поэтому там верить можно только разбору:
    он этот текст прочитал. Разбор до P3-11 языка не называет, и у его текста язык остаётся
    неизвестным, как было.
    """
    return lang if source in RECOGNISED_LANGUAGE_SOURCES else named_by_review


def assignment_of(
    review: Review, number: int, run_id: str, parent_run_id: str, meeting_lang: str | None
) -> Assignment:
    if review.owner_lang is None:
        raise ReviewUnusable(
            f"в разборе {parent_run_id} нет owner_lang: файл правили руками, повторите разбор"
        )
    if not 1 <= number <= len(review.tasks):
        raise ReviewUnusable(
            f"в разборе {parent_run_id} поручений {len(review.tasks)}, а номер {number}"
        )
    return Assignment(
        run_id=run_id,
        parent_run_id=parent_run_id,
        number=number,
        meeting_lang=meeting_lang,
        owner_lang=review.owner_lang,
        task=review.tasks[number - 1],
    )


def schema_problems(steps_json: str) -> list[str]:
    try:
        parsed = json.loads(steps_json)
    except json.JSONDecodeError as error:
        return [f"не разбирается как JSON: {error}"]
    try:
        Steps.model_validate(parsed)
    except ValidationError as error:
        return [
            f"{'.'.join(str(part) for part in problem['loc'])}: {problem['msg']}"
            for problem in error.errors()
        ]
    return []


def pairs_by_place(steps: Steps) -> list[tuple[str, Pair]]:
    return [
        ("title", steps.title),
        ("summary", steps.summary),
        *((f"steps.{index}", pair) for index, pair in enumerate(steps.steps)),
        *((f"open_questions.{index}", pair) for index, pair in enumerate(steps.open_questions)),
    ]


def marked_numbers(text: str) -> list[int]:
    """Номера вопросов, названные пометками этого текста.

    Пометка без номера — обычные слова про неясность не из списка вопросов, и проверять её
    нечем: словаря неясностей на каждый язык проект не заводит, её держит промпт.
    """
    return [
        int(number.group(1))
        for body in MARK.findall(text)
        if (number := MARK_NUMBER.match(body.strip()))
    ]


def mark_problems(steps: Steps, questions: int) -> list[str]:
    """Претензии к номеру в пометке: он должен быть вопросом списка, и вопросом без ответа.

    Иначе владелец читает на карточке ссылку в пустоту, а шаг молча решает за того, кто поручил.
    """
    unanswered = set(steps.unanswered)
    problems = []
    for place, pair in pairs_by_place(steps):
        for side, text in (("text", pair.text), ("translation", pair.translation or "")):
            for number in marked_numbers(text):
                if not questions:
                    problems.append(
                        f"{place}.{side}: пометка называет вопрос {number}, а вопросов "
                        "не задавали"
                    )
                elif not 1 <= number <= questions:
                    problems.append(
                        f"{place}.{side}: пометка называет вопрос {number}, а вопросы "
                        f"пронумерованы от 1 до {questions}"
                    )
                elif number not in unanswered:
                    problems.append(
                        f"{place}.{side}: пометка называет вопрос {number}, а его нет в "
                        "unanswered: ответ на него есть, и уточнять нечего"
                    )
    return problems


def check_steps(steps_json: str, assignment: Assignment, questions: int) -> list[str]:
    """Претензии к ответу стадии. Перевод проверяется, только если язык встречи известен.

    `questions` это длина общего списка вопросов, тимлиду и от ресёрча: номера берутся из него.
    """
    problems = schema_problems(steps_json)
    if problems:
        return problems
    steps = Steps.model_validate_json(steps_json)
    problems = [
        f"unanswered: вопроса {number} нет, вопросы пронумерованы от 1 до {questions}"
        if questions
        else f"unanswered: вопроса {number} нет, вопросов не задавали"
        for number in steps.unanswered
        if not 1 <= number <= questions
    ]
    problems += mark_problems(steps, questions)
    meeting_lang, owner_lang = assignment.meeting_lang, assignment.owner_lang
    if meeting_lang is None or meeting_lang == owner_lang:
        return problems
    return problems + [
        f"{place}.translation: перевод пуст, а встреча на {meeting_lang}, язык владельца "
        f"{owner_lang}"
        for place, pair in pairs_by_place(steps)
        if not (pair.translation and pair.translation.strip())
    ]


def stamp_steps(
    steps_json: str,
    assignment: Assignment,
    title: Pair,
    asked: list[Pair],
    found: list[Pair],
    answered: bool,
) -> str:
    """Готовые шаги: номер прогона, языки, название, вопросы и сказанное на встрече вписаны кодом.

    Всё, что модель прислала в этих полях, затирается: дословное переносит только код. Название
    одно на вопросы и чек-лист, его написала стадия вопросов. Нумерация общая: сначала вопросы
    тимлиду (`asked`), затем открытые ресёрчем (`found`).

    Если ответа тимлида не было, без ответа остались все его вопросы, что бы ни решила модель;
    если был, какие он закрыл, решает она. Вопросы ресёрча открыты всегда: ресёрч идёт после
    ответа, тимлид их не видел, и закрытый такой вопрос на карточке был бы неправдой.
    """
    steps = Steps.model_validate_json(steps_json)
    steps.title = title
    numbers = range(1, len(asked) + len(found) + 1)
    by_teamlead = set(range(1, len(asked) + 1))
    by_research = set(numbers) - by_teamlead
    still_open = set(steps.unanswered) if answered else by_teamlead
    steps.unanswered = sorted(still_open | by_research)
    steps.questions = [
        AskedQuestion(
            number=number,
            text=question.text,
            translation=question.translation,
            origin="clarify" if number in by_teamlead else "research",
            answered=number not in steps.unanswered,
        )
        for number, question in zip(numbers, [*asked, *found], strict=True)
    ]
    task = assignment.task
    steps.run_id = assignment.run_id
    steps.owner_lang = assignment.owner_lang
    steps.meeting_lang = assignment.meeting_lang
    steps.task = TaskRef(
        parent_run_id=assignment.parent_run_id,
        number=assignment.number,
        ticket_key=task.ticket_key,
        ticket_url=task.ticket_url,
        deadline=task.deadline,
        constraints=task.constraints,
        acceptance=task.acceptance,
        do_not=task.do_not,
        ask_back=task.ask_back,
    )
    return steps.model_dump_json(indent=2) + "\n"
