"""Шаги одного поручения: inputs/assignment.json и outputs/steps.json. См. SPEC.md §5, §7.

Вход собирает код из разбора родителя, выход пишет модель, а всё дословное из разбора, языки и
номер прогона вписывает код после проверок, тем же приёмом, что `owner_lang` в review.json.
Шаг это пункт чек-листа внутри одной карточки, поэтому ни оценки, ни метки, ни зависимостей у
него нет: повесить их на пункт чек-листа в Trello нельзя, а заполненные для порядка поля были бы
выдуманными данными.
"""

import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.review import NOT_BLANK, AskBack, Review, Said, Task

MAX_STEPS = 10


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
    deadline: Said | None = None
    constraints: list[Said] = Field(default_factory=list)
    do_not: list[Said] = Field(default_factory=list)
    ask_back: list[AskBack] = Field(default_factory=list)


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


class Assignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    parent_run_id: str
    number: int = Field(ge=1)
    # Язык, который назвало распознавание. У текста его никто не распознавал, и это null, а не
    # DEFAULT_LANG: иначе стадия получила бы выдуманный язык встречи.
    meeting_lang: str | None
    owner_lang: str
    task: Task


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


def check_steps(steps_json: str, assignment: Assignment) -> list[str]:
    """Претензии к ответу стадии. Перевод проверяется, только если язык встречи известен."""
    problems = schema_problems(steps_json)
    if problems:
        return problems
    meeting_lang, owner_lang = assignment.meeting_lang, assignment.owner_lang
    if meeting_lang is None or meeting_lang == owner_lang:
        return []
    return [
        f"{place}.translation: перевод пуст, а встреча на {meeting_lang}, язык владельца "
        f"{owner_lang}"
        for place, pair in pairs_by_place(Steps.model_validate_json(steps_json))
        if not (pair.translation and pair.translation.strip())
    ]


def stamp_steps(steps_json: str, assignment: Assignment) -> str:
    """Готовые шаги: номер прогона, языки и сказанное на встрече вписаны кодом из входа.

    Всё, что модель прислала в этих полях, затирается: дословное переносит только код.
    """
    steps = Steps.model_validate_json(steps_json)
    task = assignment.task
    steps.run_id = assignment.run_id
    steps.owner_lang = assignment.owner_lang
    steps.meeting_lang = assignment.meeting_lang
    steps.task = TaskRef(
        parent_run_id=assignment.parent_run_id,
        number=assignment.number,
        deadline=task.deadline,
        constraints=task.constraints,
        do_not=task.do_not,
        ask_back=task.ask_back,
    )
    return steps.model_dump_json(indent=2) + "\n"
