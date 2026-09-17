"""Контракт шагов поручения (app/steps.py).

Немецкие шаги, название и суть в fixtures/steps_de.json составлены вручную, как и
transcript_meeting_de.md: живого прогона стадии steps ещё не было. assignment_de.json собран
кодом из review_de.json, и первый тест это проверяет.
"""

import json
from typing import Any

import pytest

from app.review import Review
from app.steps import (
    MAX_STEPS,
    Assignment,
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


def assignment(meeting_lang: str | None = "de") -> Assignment:
    return Assignment.model_validate_json(ASSIGNMENT_DE).model_copy(
        update={"meeting_lang": meeting_lang}
    )


def model_answer() -> dict[str, Any]:
    """Ответ модели: без полей, которые вписывает код."""
    data: dict[str, Any] = json.loads(STEPS_DE)
    for stamped in ("run_id", "owner_lang", "meeting_lang", "task"):
        del data[stamped]
    return data


def problems_of(data: dict[str, Any], meeting_lang: str | None = "de") -> list[str]:
    return check_steps(json.dumps(data, ensure_ascii=False), assignment(meeting_lang))


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
    assert check_steps(STEPS_DE, assignment()) == []
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
    assert check_steps("{", assignment())[0].startswith("не разбирается как JSON")


def test_the_stamp_writes_the_run_the_languages_and_what_was_said_over_the_model() -> None:
    data = model_answer()
    data["run_id"] = "выдумала модель"
    data["meeting_lang"] = "en"
    data["task"] = {"parent_run_id": "чужой", "number": 9, "do_not": []}

    stamped = Steps.model_validate_json(stamp_steps(json.dumps(data), assignment()))

    task = Assignment.model_validate_json(ASSIGNMENT_DE).task
    assert (stamped.run_id, stamped.owner_lang, stamped.meeting_lang) == (CHILD, "ru", "de")
    assert stamped.task is not None
    assert (stamped.task.parent_run_id, stamped.task.number) == (PARENT, 1)
    assert stamped.task.deadline == task.deadline
    assert stamped.task.constraints == task.constraints
    assert stamped.task.do_not == task.do_not
    assert stamped.task.ask_back == task.ask_back


def test_the_steps_fixture_is_the_stamped_model_answer() -> None:
    assert stamp_steps(json.dumps(model_answer()), assignment()) == STEPS_DE


def test_the_stamp_carries_the_ticket_and_its_acceptance_criteria_from_the_review() -> None:
    """review_ticket_de.json написан вручную по составленному вручную тексту тикета."""
    review = Review.model_validate_json(
        (FIXTURES / "review_ticket_de.json").read_text(encoding="utf-8")
    )
    ticket_assignment = assignment_of(review, 1, CHILD, PARENT, None)

    stamped = Steps.model_validate_json(
        stamp_steps(json.dumps(model_answer()), ticket_assignment)
    )

    assert stamped.task is not None
    task = review.tasks[0]
    assert (stamped.task.ticket_key, stamped.task.ticket_url) == (
        "ABC-123",
        "https://jira.example.com/browse/ABC-123",
    )
    assert stamped.task.acceptance == task.acceptance
