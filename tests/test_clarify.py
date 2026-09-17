"""Контракт вопросов для тимлида (app/clarify.py).

Немецкие вопросы, их перевод и пояснения в fixtures/clarify_de.json составлены вручную: живого
прогона стадии clarify ещё не было. Поручение взято из assignment_de.json, собранного кодом.
"""

import json
import re
from typing import Any

import pytest

from app.clarify import MAX_QUESTIONS, Clarify, check_clarify, has_questions, stamp_clarify
from app.stages import COMMANDS_DIR
from app.steps import Assignment
from tests.helpers import FIXTURES

CLARIFY_DE = (FIXTURES / "clarify_de.json").read_text(encoding="utf-8")
ASSIGNMENT_DE = (FIXTURES / "assignment_de.json").read_text(encoding="utf-8")
HEARD = "Kannst du das bis Freitag machen?"


def assignment(meeting_lang: str | None = "de") -> Assignment:
    return Assignment.model_validate_json(ASSIGNMENT_DE).model_copy(
        update={"meeting_lang": meeting_lang}
    )


def model_answer() -> dict[str, Any]:
    """Ответ модели: без полей, которые вписывает код."""
    data: dict[str, Any] = json.loads(CLARIFY_DE)
    for stamped in ("run_id", "meeting_lang", "owner_lang", "address_in_text"):
        del data[stamped]
    return data


def problems_of(data: dict[str, Any], meeting_lang: str | None = "de") -> list[str]:
    return check_clarify(json.dumps(data, ensure_ascii=False), assignment(meeting_lang))


def test_the_german_questions_pass_the_check_and_are_what_the_stamp_writes() -> None:
    assert problems_of(model_answer()) == []
    assert stamp_clarify(json.dumps(model_answer(), ensure_ascii=False), assignment()) == CLARIFY_DE


def test_more_than_seven_questions_is_a_problem() -> None:
    data = model_answer()
    data["questions"] = data["questions"] * 3

    problems = problems_of(data)

    assert len(problems) == 1 and problems[0].startswith("questions:")
    assert MAX_QUESTIONS == 7


def test_a_blank_question_is_a_problem() -> None:
    data = model_answer()
    data["questions"][1]["text"] = "  "

    problems = problems_of(data)

    assert len(problems) == 1 and problems[0].startswith("questions.1.text:")


def test_a_question_longer_than_two_sentences_allow_is_a_problem() -> None:
    data = model_answer()
    data["questions"][0]["text"] = "Gilt das? " * 40

    assert problems_of(data)[0].startswith("questions.0.text:")


def test_a_question_without_why_is_a_problem_whatever_the_language() -> None:
    """Без «зачем» владельцу нечем отбирать вопросы, и это не зависит от языка встречи."""
    data = model_answer()
    data["questions"][2]["why"] = ""

    assert problems_of(data, meeting_lang=None)[0].startswith("questions.2.why:")


def test_an_empty_translation_of_a_german_meeting_is_named_by_its_place() -> None:
    data = model_answer()
    data["questions"][0]["translation"] = " "
    data["title"]["translation"] = None

    assert problems_of(data) == [
        "title.translation: перевод пуст, а встреча на de, язык владельца ru",
        "questions.0.translation: перевод пуст, а встреча на de, язык владельца ru",
    ]


@pytest.mark.parametrize("meeting_lang", [None, "ru"])
def test_no_translation_is_demanded_when_the_meeting_language_is_unknown_or_the_owners(
    meeting_lang: str | None,
) -> None:
    data = model_answer()
    data["title"]["translation"] = None
    for question in data["questions"]:
        question["translation"] = None

    assert problems_of(data, meeting_lang) == []


def test_the_same_question_twice_is_a_problem() -> None:
    """Вопрос из ask_back разбора входит в список один раз, сколько бы источников его ни дали."""
    data = model_answer()
    data["questions"].append(dict(data["questions"][0]))

    assert problems_of(data) == ["questions.3: тот же вопрос, что questions.0: оставь один"]


def test_no_questions_is_a_valid_answer_and_asks_nothing() -> None:
    data = model_answer()
    data["questions"] = []

    assert problems_of(data) == []
    assert not has_questions(Clarify.model_validate(data))
    assert has_questions(Clarify.model_validate_json(CLARIFY_DE))


def test_an_informal_address_needs_a_quote_that_was_said() -> None:
    data = model_answer()
    data["address"] = "informal"
    data["address_quote"] = "Kannst du das bitte heute machen?"

    [problem] = problems_of(data)

    assert problem.startswith("address_quote:")


def test_an_informal_address_with_a_quote_that_was_said_is_clean_and_stamped_found() -> None:
    data = model_answer()
    data["address"] = "informal"
    data["address_quote"] = HEARD

    assert problems_of(data) == []
    stamped = Clarify.model_validate_json(stamp_clarify(json.dumps(data), assignment()))
    assert stamped.address_in_text is True


@pytest.mark.parametrize("address", ["formal", "neutral"])
def test_a_formal_or_neutral_address_needs_no_quote(address: str) -> None:
    data = model_answer()
    data["address"] = address

    assert problems_of(data) == []


def test_a_field_the_first_draft_had_is_a_schema_error() -> None:
    """Вводной фразы у списка нет: бот тимлиду не пишет, а письмо не разрезать на вопросы."""
    data = model_answer()
    data["lead"] = "Kurze Rückfragen zum Login-Formular:"

    assert problems_of(data) == ["lead: Extra inputs are not permitted"]


def test_broken_json_is_a_problem() -> None:
    assert check_clarify("{", assignment())[0].startswith("не разбирается как JSON")


def test_the_stamp_writes_the_run_and_the_languages_over_the_model() -> None:
    data = model_answer()
    data["run_id"] = "выдумала модель"
    data["meeting_lang"] = "en"
    data["address_in_text"] = True

    stamped = Clarify.model_validate_json(stamp_clarify(json.dumps(data), assignment(None)))

    assert (stamped.run_id, stamped.meeting_lang, stamped.owner_lang) == (
        "7b2e0d91c4a3f615",
        None,
        "ru",
    )
    assert stamped.address_in_text is False


def test_the_example_in_the_prompt_passes_the_schema_without_the_fields_the_code_writes() -> None:
    """Пример в /clarify показывает форму: разойдись он со схемой, модель училась бы на отказе."""
    prompt = (COMMANDS_DIR / "clarify.md").read_text(encoding="utf-8")
    example = re.search(r"```json\n(.*?)```", prompt, re.DOTALL)

    assert example is not None
    for stamped in ("run_id", "meeting_lang", "owner_lang", "address_in_text"):
        assert f'"{stamped}"' not in example.group(1)
    Clarify.model_validate_json(example.group(1))
