import json
from pathlib import Path
from typing import Any

import pytest

from app.review import Review, check_review, stamp_review, unmatched_originals

FIXTURES = Path(__file__).parent.parent / "fixtures"
MEETING_DE = (FIXTURES / "transcript_meeting_de.md").read_text(encoding="utf-8")
REVIEW_DE = (FIXTURES / "review_de.json").read_text(encoding="utf-8")
REVIEW_NONE = (FIXTURES / "review_none.json").read_text(encoding="utf-8")

# Короткий разговор без поручений под темы из review_none.json. Составлен вручную, как и
# transcript_meeting_de.md: живой записи встречи без поручений у нас нет.
SMALL_TALK_DE = (
    "---\nsource: file\nduration: 21\nlang: de\nconsent_confirmed: true\n---\n\n"
    "Wer ist eigentlich im Oktober im Urlaub? Und die neue Kaffeemaschine in der Küche ist da.\n"
)


def review_data(text: str = REVIEW_DE) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(text)
    return data


def problems_of(data: dict[str, Any], transcript: str = MEETING_DE) -> list[str]:
    return check_review(json.dumps(data, ensure_ascii=False), transcript, "ru")


def with_quote(original: str) -> Review:
    data = review_data()
    data["tasks"][0]["quotes"] = [{"original": original, "translation": "перевод"}]
    data["tasks"][0]["deadline"] = None
    data["tasks"][0]["constraints"] = []
    data["tasks"][0]["do_not"] = []
    data["tasks"] = data["tasks"][:1]
    return Review.model_validate(data)


def unmatched(original: str, transcript: str = MEETING_DE) -> list[str]:
    return unmatched_originals(with_quote(original), transcript)


def test_the_german_meeting_review_passes_the_check() -> None:
    """transcript_meeting_de.md составлена вручную: живой немецкой записи встречи ещё не было."""
    assert check_review(REVIEW_DE, MEETING_DE, "ru") == []


def test_a_review_without_tasks_passes_the_check() -> None:
    assert check_review(REVIEW_NONE, SMALL_TALK_DE, "ru") == []


def test_a_quote_that_nobody_said_is_reported_with_its_text() -> None:
    assert unmatched("Das Login bauen wir komplett neu.") == ["Das Login bauen wir komplett neu."]


def test_other_quotes_and_punctuation_around_the_same_words_still_match() -> None:
    assert unmatched("„bitte die API nicht anfassen“ — die gehört dem Backend Team!") == []


def test_a_compound_word_split_by_the_recognition_matches_joined_and_hyphenated() -> None:
    """Распознавание пишет «Login Formular» через пробел, модель склеивает слово."""
    assert "Login Formular" in MEETING_DE
    assert unmatched("Erstens das LoginFormular.") == []
    assert unmatched("Erstens das Login-Formular.") == []


def test_a_fixed_typo_does_not_match() -> None:
    """Дословно так сказано не было: распознанное «Fehlermeldung» при «sind» модель поправила."""
    fixed = "Die Fehlermeldungen auf der Kontoseite sind noch auf Englisch."

    assert unmatched(fixed) == [fixed]


def test_a_fragment_with_an_ellipsis_is_found_part_by_part_in_order() -> None:
    assert unmatched("Erstens das Login Formular … bis Freitag machen") == []
    assert unmatched("Erstens das Login Formular... bis Freitag machen") == []
    assert unmatched("bis Freitag machen … Erstens das Login Formular") != []


def test_a_word_from_the_frontmatter_does_not_confirm_a_fragment() -> None:
    assert "consent_confirmed" in MEETING_DE

    assert unmatched("consent_confirmed: true") != []


def test_an_empty_translation_of_a_recording_is_reported() -> None:
    data = review_data()
    data["tasks"][0]["quotes"][1]["translation"] = None
    data["tasks"][0]["ask_back"][0]["translation"] = " "

    assert problems_of(data) == [
        "tasks.0.quotes.1.translation: перевод пуст, а запись на de, язык владельца ru",
        "tasks.0.ask_back.0.translation: перевод пуст, а запись на de, язык владельца ru",
    ]


def test_an_empty_translation_of_a_text_is_left_to_the_prompt() -> None:
    """У текста язык не распознан, а взят из DEFAULT_LANG: требовать перевод по нему нельзя."""
    data = review_data()
    data["tasks"][0]["quotes"][1]["translation"] = None
    typed = MEETING_DE.replace("source: file", "source: text")

    assert problems_of(data, typed) == []


def test_no_translation_is_needed_when_the_meeting_speaks_the_owner_language() -> None:
    data = review_data()
    data["tasks"][0]["quotes"][1]["translation"] = None

    assert problems_of(data, MEETING_DE.replace("lang: de", "lang: ru")) == []


def test_topics_next_to_tasks_are_reported() -> None:
    data = review_data()
    data["topics"] = ["Отпуска"]

    assert problems_of(data) == ["topics: темы пишутся только при пустом tasks, а поручения есть"]


@pytest.mark.parametrize("quotes", [[], [{"original": "bis Freitag"}] * 4])
def test_a_task_needs_one_to_three_quotes(quotes: list[dict[str, str]]) -> None:
    data = review_data()
    data["tasks"][0]["quotes"] = quotes

    problems = problems_of(data)

    assert len(problems) == 1
    assert problems[0].startswith("tasks.0.quotes:")


@pytest.mark.parametrize("summary", ["", "  \n"])
def test_a_blank_summary_is_a_schema_problem(summary: str) -> None:
    data = review_data()
    data["tasks"][0]["summary"] = summary

    problems = problems_of(data)

    assert len(problems) == 1
    assert problems[0].startswith("tasks.0.summary:")


def test_a_missing_summary_is_a_schema_problem() -> None:
    data = review_data()
    del data["tasks"][1]["summary"]

    assert problems_of(data) == ["tasks.1.summary: Field required"]


def test_a_review_without_tasks_is_a_schema_problem() -> None:
    """Пустоту разбора объявляет пустой список, а не отсутствие поля."""
    assert problems_of({"topics": []}) == ["tasks: Field required"]


def test_text_that_is_not_json_is_reported() -> None:
    problems = check_review("вот разбор, но без json", MEETING_DE, "ru")

    assert len(problems) == 1
    assert problems[0].startswith("не разбирается как JSON")


def test_the_stamp_marks_unmatched_fragments_and_overwrites_what_the_model_claimed() -> None:
    data = review_data()
    data["owner_lang"] = None
    data["tasks"][0]["quotes"][0] = {
        "original": "Das Login bauen wir komplett neu.",
        "translation": "Логин переделаем с нуля.",
        "in_transcript": True,
    }
    data["tasks"][0]["deadline"]["in_transcript"] = None

    stamped = Review.model_validate_json(
        stamp_review(json.dumps(data, ensure_ascii=False), MEETING_DE, "ru")
    )

    first = stamped.tasks[0]
    assert stamped.owner_lang == "ru"
    assert first.quotes[0].in_transcript is False
    assert first.quotes[1].in_transcript is True
    assert first.deadline is not None and first.deadline.in_transcript is True


def test_the_fixture_is_what_the_stamp_writes() -> None:
    """review_de.json лежит в том виде, в каком его оставляет стадия: пометки поставила сверка."""
    assert stamp_review(REVIEW_DE, MEETING_DE, "ru") == REVIEW_DE
