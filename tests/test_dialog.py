import pytest

from app.config import settings
from app.dialog import MAX_QUESTION_CHARACTERS, budget_spent, check_question

QUESTION = "Сколько человек в команде и как часто они смотрят в Trello?\n"


def test_a_question_of_one_sentence_is_what_the_stage_is_asked_for() -> None:
    assert check_question(QUESTION) == []


def test_a_question_without_a_single_word_is_sent_back_for_repair() -> None:
    """Пустой вопрос доехал бы до человека сообщением без текста, и Telegram его не отправит."""
    assert check_question("\n   \n") == [
        "вопрос пустой: человеку ушло бы сообщение без единого слова"
    ]


def test_a_question_longer_than_a_message_is_sent_back_for_repair() -> None:
    problems = check_question("а" * (MAX_QUESTION_CHARACTERS + 1))

    assert problems == [
        f"вопрос длиннее {MAX_QUESTION_CHARACTERS} символов, столько Telegram не отправит: "
        "нужно одно предложение"
    ]


def test_the_budget_of_questions_is_the_one_from_the_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """На стенде число меньше, чем в спокойной работе: человек ждёт у стола."""
    monkeypatch.setattr(settings, "max_brief_questions", 3)

    assert not budget_spent(2)
    assert budget_spent(3)
