"""Контракт шагов поручения (app/steps.py).

Немецкие шаги, название и суть в fixtures/steps_de.json составлены вручную, как и
transcript_meeting_de.md: живого прогона стадии steps ещё не было. Так же вручную составлены
вопросы clarify_de.json и ответы тимлида answers_de.md и answers_de_partial.md. assignment_de.json
собран кодом из review_de.json, и первый тест это проверяет.
"""

import json
from typing import Any

import pytest

from app.answers import read_answers
from app.clarify import Clarify
from app.review import Review
from app.steps import (
    MAX_STEPS,
    Assignment,
    Pair,
    ReviewUnusable,
    Steps,
    assignment_of,
    check_steps,
    stamp_steps,
)
from tests.helpers import FIXTURES

REVIEW_DE = (FIXTURES / "review_de.json").read_text(encoding="utf-8")
ASSIGNMENT_DE = (FIXTURES / "assignment_de.json").read_text(encoding="utf-8")
STEPS_DE = (FIXTURES / "steps_de.json").read_text(encoding="utf-8")
PARENT = "3f9c1a7e5b2d8c40"
CHILD = "7b2e0d91c4a3f615"
CLARIFY = Clarify.model_validate_json((FIXTURES / "clarify_de.json").read_text(encoding="utf-8"))
ASKED = [Pair(text=asked.text, translation=asked.translation) for asked in CLARIFY.questions]
PARTIAL = read_answers((FIXTURES / "answers_de_partial.md").read_text(encoding="utf-8"))


def assignment(meeting_lang: str | None = "de") -> Assignment:
    return Assignment.model_validate_json(ASSIGNMENT_DE).model_copy(
        update={"meeting_lang": meeting_lang}
    )


def model_answer() -> dict[str, Any]:
    """Ответ модели: без полей, которые вписывает код."""
    data: dict[str, Any] = json.loads(STEPS_DE)
    for stamped in ("run_id", "owner_lang", "meeting_lang", "task", "questions"):
        del data[stamped]
    return data


def problems_of(data: dict[str, Any], meeting_lang: str | None = "de") -> list[str]:
    return check_steps(json.dumps(data, ensure_ascii=False), assignment(meeting_lang), len(ASKED))


def stamped(
    data: dict[str, Any],
    given: Assignment | None = None,
    answered: bool = True,
    asked: list[Pair] = ASKED,
) -> Steps:
    stamp = stamp_steps(json.dumps(data), given or assignment(), CLARIFY.title, asked, answered)
    return Steps.model_validate_json(stamp)


def test_the_assignment_fixture_is_task_one_of_the_german_review() -> None:
    review = Review.model_validate_json(REVIEW_DE)

    built = assignment_of(review, 1, CHILD, PARENT, "de")

    assert (built.number, built.owner_lang, built.meeting_lang) == (1, "ru", "de")
    assert built.task == review.tasks[0]
    assert built.model_dump_json(indent=2) + "\n" == ASSIGNMENT_DE


def test_the_assignment_carries_the_meeting_language_it_was_given() -> None:
    """У текста язык встречи никто не распознавал: null, а не DEFAULT_LANG."""
    review = Review.model_validate_json(REVIEW_DE)

    assert assignment_of(review, 2, CHILD, PARENT, None).meeting_lang is None


@pytest.mark.parametrize("number", [0, 3])
def test_a_number_outside_the_review_is_refused(number: int) -> None:
    with pytest.raises(ReviewUnusable, match=f"номер {number}"):
        assignment_of(Review.model_validate_json(REVIEW_DE), number, CHILD, PARENT, "de")


def test_a_review_without_the_owner_language_is_refused() -> None:
    """Язык владельца вписывает код после разбора, и его отсутствие значит ручную правку."""
    review = Review.model_validate_json(REVIEW_DE)
    review.owner_lang = None

    with pytest.raises(ReviewUnusable, match="owner_lang"):
        assignment_of(review, 1, CHILD, PARENT, "de")


def test_the_german_steps_pass_the_check() -> None:
    assert check_steps(STEPS_DE, assignment(), len(ASKED)) == []
    assert problems_of(model_answer()) == []


def test_an_empty_translation_of_a_german_meeting_is_named_by_its_place() -> None:
    data = model_answer()
    data["steps"][2]["translation"] = " "
    data["summary"]["translation"] = None

    assert problems_of(data) == [
        "summary.translation: перевод пуст, а встреча на de, язык владельца ru",
        "steps.2.translation: перевод пуст, а встреча на de, язык владельца ru",
    ]


@pytest.mark.parametrize("meeting_lang", [None, "ru"])
def test_no_translation_is_demanded_when_the_meeting_language_is_unknown_or_the_owners(
    meeting_lang: str | None,
) -> None:
    data = model_answer()
    for pair in (data["title"], data["summary"], *data["steps"]):
        pair["translation"] = None

    assert problems_of(data, meeting_lang) == []


def test_an_untranslated_open_question_of_a_german_meeting_is_a_problem() -> None:
    data = model_answer()
    data["open_questions"] = [{"text": "Wo liegt das Login-Formular?", "translation": None}]

    assert problems_of(data) == [
        "open_questions.0.translation: перевод пуст, а встреча на de, язык владельца ru"
    ]


@pytest.mark.parametrize("count", [0, MAX_STEPS + 1])
def test_no_steps_or_more_than_ten_is_a_problem(count: int) -> None:
    data = model_answer()
    data["steps"] = [{"text": f"Schritt {index}", "translation": "шаг"} for index in range(count)]

    problems = problems_of(data)

    assert len(problems) == 1 and problems[0].startswith("steps:")


@pytest.mark.parametrize("field", ["phases", "dod"])
def test_a_field_of_the_idea_backlog_is_a_schema_error(field: str) -> None:
    """Шаг это пункт чек-листа: оценок, зависимостей и фаз у него нет."""
    data = model_answer()
    data[field] = []

    assert problems_of(data) == [f"{field}: Extra inputs are not permitted"]


def test_a_blank_step_is_a_problem() -> None:
    data = model_answer()
    data["steps"][0]["text"] = "   "

    problems = problems_of(data)

    assert len(problems) == 1 and problems[0].startswith("steps.0.text:")


def test_broken_json_is_a_problem() -> None:
    assert check_steps("{", assignment(), len(ASKED))[0].startswith("не разбирается как JSON")


def test_the_stamp_writes_the_run_the_languages_and_what_was_said_over_the_model() -> None:
    data = model_answer()
    data["run_id"] = "выдумала модель"
    data["meeting_lang"] = "en"
    data["task"] = {"parent_run_id": "чужой", "number": 9, "do_not": []}

    result = stamped(data)

    task = Assignment.model_validate_json(ASSIGNMENT_DE).task
    assert (result.run_id, result.owner_lang, result.meeting_lang) == (CHILD, "ru", "de")
    assert result.task is not None
    assert (result.task.parent_run_id, result.task.number) == (PARENT, 1)
    assert result.task.deadline == task.deadline
    assert result.task.constraints == task.constraints
    assert result.task.do_not == task.do_not
    assert result.task.ask_back == task.ask_back


def test_the_steps_fixture_is_the_stamped_model_answer() -> None:
    """Шаги написаны по частичному ответу: тимлид ответил только на вопрос о библиотеке."""
    assert PARTIAL.status == "answered"
    answer = json.dumps(model_answer(), ensure_ascii=False)

    assert stamp_steps(answer, assignment(), CLARIFY.title, ASKED, True) == STEPS_DE


def test_the_stamp_carries_the_ticket_and_its_acceptance_criteria_from_the_review() -> None:
    """review_ticket_de.json написан вручную по составленному вручную тексту тикета."""
    review = Review.model_validate_json(
        (FIXTURES / "review_ticket_de.json").read_text(encoding="utf-8")
    )
    ticket_assignment = assignment_of(review, 1, CHILD, PARENT, None)

    result = stamped(model_answer(), ticket_assignment)

    assert result.task is not None
    task = review.tasks[0]
    assert (result.task.ticket_key, result.task.ticket_url) == (
        "ABC-123",
        "https://jira.example.com/browse/ABC-123",
    )
    assert result.task.acceptance == task.acceptance


def test_an_unanswered_number_outside_the_questions_is_a_problem() -> None:
    data = model_answer()
    data["unanswered"] = [1, 9]

    assert problems_of(data) == [
        "unanswered: вопроса 9 нет, вопросы тимлиду пронумерованы от 1 до 3"
    ]


def test_any_unanswered_number_is_a_problem_when_nothing_was_asked() -> None:
    data = model_answer()
    data["unanswered"] = [1]

    problems = check_steps(json.dumps(data), assignment(), 0)

    assert problems == ["unanswered: вопроса 1 нет, тимлиду вопросов не задавали"]


@pytest.mark.parametrize("unanswered", [[2, 3], [1, 2, 3], []])
def test_partial_answer_leaves_any_subset_unanswered(unanswered: list[int]) -> None:
    """Тимлид ответил «zu 1» на второй вопрос бота: какие вопросы закрыты, решает стадия по смыслу.

    Код не сверяет ответ с номерами: любое подмножество валидно, и при пришедшем ответе штамп
    оставляет его как есть.
    """
    data = model_answer()
    data["unanswered"] = unanswered

    assert problems_of(data) == []
    result = stamped(data)
    assert result.unanswered == unanswered
    assert [question.answered for question in result.questions] == [
        number not in unanswered for number in (1, 2, 3)
    ]


def test_without_an_answer_every_question_stays_unanswered_whatever_the_model_said() -> None:
    """«Продолжить без ответов» и неотправленные вопросы: ответа не было ни на один."""
    data = model_answer()
    data["unanswered"] = [2]

    result = stamped(data, answered=False)

    assert result.unanswered == [1, 2, 3]
    assert not any(question.answered for question in result.questions)


def test_nothing_asked_leaves_no_questions_and_nothing_unanswered() -> None:
    data = model_answer()
    data["unanswered"] = []

    result = stamped(data, answered=False, asked=[])

    assert (result.unanswered, result.questions) == ([], [])


def test_the_questions_are_numbered_and_copied_by_the_code_over_the_model() -> None:
    data = model_answer()
    data["questions"] = [
        {"number": 1, "text": "переписала модель", "origin": "clarify", "answered": True}
    ]

    result = stamped(data)

    assert [(asked.number, asked.text, asked.origin) for asked in result.questions] == [
        (number, pair.text, "clarify") for number, pair in enumerate(ASKED, start=1)
    ]


def test_the_title_of_the_questions_overwrites_the_title_the_model_sent() -> None:
    """Вопросы и чек-лист идут под одним названием, и написала его стадия вопросов."""
    data = model_answer()
    data["title"] = {"text": "Ein anderer Titel", "translation": "Другое название"}

    assert stamped(data).title == CLARIFY.title


def test_a_note_to_the_teamlead_is_a_schema_error() -> None:
    """Текст на воротах пишется владельцу: фразы-обращения к тимлиду у шагов нет."""
    data = model_answer()
    data["teamlead_note"] = {"text": "Mein Plan, passt das so?", "translation": None}

    assert problems_of(data) == ["teamlead_note: Extra inputs are not permitted"]
