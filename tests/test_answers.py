"""Файл ответа тимлида (app/answers.py).

Ответы в fixtures/answers_de.md и answers_de_partial.md составлены вручную: живого ответа тимлида
через бота ещё не было.
"""

from datetime import UTC, datetime
from typing import get_args

import pytest
from pydantic import ValidationError

from app.answers import Answers, AnswersStatus, answers_file, read_answers
from tests.helpers import FIXTURES

RECEIVED = datetime(2026, 9, 19, 8, 14, tzinfo=UTC)


def test_the_answer_fixtures_are_what_the_file_writes() -> None:
    for name in ("answers_de.md", "answers_de_partial.md"):
        content = (FIXTURES / name).read_text(encoding="utf-8")

        answers = read_answers(content)

        assert (answers.status, answers.received_at) == ("answered", RECEIVED)
        assert answers_file(answers) == content


def test_an_answer_keeps_its_text_as_it_was_pasted() -> None:
    """Какие вопросы закрыты, решают шаги по смыслу: нумерация тимлида и приветствие остаются."""
    text = "Hallo,\nzu 1: Nur das Login-Formular.\n\n2) Frei wählbar."

    written = answers_file(Answers(status="answered", received_at=RECEIVED, text=text))

    assert read_answers(written).text == text


@pytest.mark.parametrize("status", ["without_answers", "not_sent", "nothing_asked"])
def test_a_status_without_an_answer_has_neither_a_time_nor_a_text(status: AnswersStatus) -> None:
    written = answers_file(Answers(status=status))

    assert written == f"---\nstatus: {status}\n---\n"
    assert read_answers(written) == Answers(status=status)


def test_every_status_is_written_and_read_back() -> None:
    for status in get_args(AnswersStatus):
        answers = (
            Answers(status=status, received_at=RECEIVED, text="Ja.")
            if status == "answered"
            else Answers(status=status)
        )

        assert read_answers(answers_file(answers)) == answers


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "answered"},
        {"status": "answered", "received_at": RECEIVED, "text": "  "},
        {"status": "not_sent", "received_at": RECEIVED},
        {"status": "without_answers", "text": "Ja."},
    ],
)
def test_a_time_and_a_text_belong_to_an_answer_and_only_to_it(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="received_at и текст есть у answered"):
        Answers.model_validate(fields)
