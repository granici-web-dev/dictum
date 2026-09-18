import json
import re
from pathlib import Path
from typing import Any

import pytest

from app.ingest import build_transcript
from app.review import Review, check_review, stamp_review, unmatched_originals, unmatched_tickets
from app.stages import COMMANDS_DIR

FIXTURES = Path(__file__).parent.parent / "fixtures"
MEETING_DE = (FIXTURES / "transcript_meeting_de.md").read_text(encoding="utf-8")
REVIEW_DE = (FIXTURES / "review_de.json").read_text(encoding="utf-8")
REVIEW_NONE = (FIXTURES / "review_none.json").read_text(encoding="utf-8")
# Текст тикета составлен вручную: ключ ABC-123 и адрес jira.example.com ничьи, это не копия
# настоящего тикета. Разбор к нему тоже написан руками и проштампован кодом.
TICKET_DE = build_transcript(
    (FIXTURES / "ticket_de.md").read_text(encoding="utf-8"),
    "de",
    "5c0e4b7a9d2f1e36",
    "text",
    None,
    None,
)
REVIEW_TICKET_DE = (FIXTURES / "review_ticket_de.json").read_text(encoding="utf-8")
TICKET_URL = "https://jira.example.com/browse/ABC-123"

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


def test_a_text_without_a_named_language_is_a_problem_and_a_recording_without_one_is_not() -> None:
    """У записи язык называет Whisper, а у текста никто: без него шаги пишутся на любом языке."""
    written = review_data(REVIEW_TICKET_DE)
    del written["meeting_lang"]
    recorded = review_data()
    del recorded["meeting_lang"]

    assert problems_of(written, TICKET_DE) == [
        "meeting_lang: язык этого текста не распознавал никто, назови его сам двумя буквами"
    ]
    assert problems_of(recorded) == []


@pytest.mark.parametrize("named", ["немецкий", "de-DE", "DE", "deu", ""])
def test_a_language_that_is_not_a_two_letter_code_is_a_problem(named: str) -> None:
    """Стадии шагов и вопросов сравнивают его с `owner_lang`: «de-DE» там не язык."""
    data = review_data()
    data["meeting_lang"] = named

    assert problems_of(data)[0].startswith("meeting_lang:")


def test_the_fixture_is_what_the_stamp_writes() -> None:
    """review_de.json лежит в том виде, в каком его оставляет стадия: пометки поставила сверка."""
    assert stamp_review(REVIEW_DE, MEETING_DE, "ru") == REVIEW_DE


def test_the_example_in_the_prompt_passes_the_schema_without_the_fields_the_code_writes() -> None:
    """Пример в /review показывает форму: разойдись он со схемой, модель училась бы на отказе."""
    prompt = (COMMANDS_DIR / "review.md").read_text(encoding="utf-8")
    example = re.search(r"```json\n(.*?)```", prompt, re.DOTALL)

    assert example is not None
    assert "owner_lang" not in example.group(1)
    assert "in_transcript" not in example.group(1)
    Review.model_validate_json(example.group(1))


def ticket_review(**fields: object) -> str:
    data = review_data(REVIEW_TICKET_DE)
    data["tasks"][0].update(fields)
    return json.dumps(data, ensure_ascii=False)


def test_the_ticket_review_is_what_the_stamp_writes_and_passes_the_check() -> None:
    """ticket_de.md и review_ticket_de.json составлены вручную, ключ и адрес ничьи."""
    assert check_review(REVIEW_TICKET_DE, TICKET_DE, "ru") == []
    assert stamp_review(REVIEW_TICKET_DE, TICKET_DE, "ru") == REVIEW_TICKET_DE
    [task] = Review.model_validate_json(REVIEW_TICKET_DE).tasks
    assert (task.ticket_key, task.ticket_url, len(task.acceptance)) == ("ABC-123", TICKET_URL, 3)


def test_ticket_key_that_is_not_in_the_text_is_dropped() -> None:
    answer = ticket_review(ticket_key="ABC-124")

    assert unmatched_tickets(Review.model_validate_json(answer), TICKET_DE) == ["ABC-124"]
    [task] = Review.model_validate_json(stamp_review(answer, TICKET_DE, "ru")).tasks
    assert task.ticket_key is None
    assert task.ticket_url == TICKET_URL


@pytest.mark.parametrize("key", ["ABC-12", "BC-123"])
def test_a_ticket_key_found_only_inside_a_longer_key_is_dropped(key: str) -> None:
    [task] = Review.model_validate_json(
        stamp_review(ticket_review(ticket_key=key), TICKET_DE, "ru")
    ).tasks

    assert task.ticket_key is None


@pytest.mark.parametrize(
    "url",
    ["https://jira.example.com/browse/ABC-12", "https://jira.example.org/browse/ABC-123"],
)
def test_a_ticket_url_that_is_not_in_the_text_letter_for_letter_is_dropped(url: str) -> None:
    answer = ticket_review(ticket_url=url)

    assert unmatched_tickets(Review.model_validate_json(answer), TICKET_DE) == [url]
    [task] = Review.model_validate_json(stamp_review(answer, TICKET_DE, "ru")).tasks
    assert (task.ticket_key, task.ticket_url) == ("ABC-123", None)


def test_a_ticket_key_from_the_frontmatter_alone_is_not_in_the_text() -> None:
    header_only = TICKET_DE.replace("ABC-123", "XYZ-1").replace(
        "lang: de", "lang: de\nnote: ABC-123"
    )

    assert unmatched_tickets(Review.model_validate_json(REVIEW_TICKET_DE), header_only) == [
        "ABC-123",
        TICKET_URL,
    ]


def test_an_acceptance_criterion_nobody_wrote_is_marked_like_any_fragment() -> None:
    invented = {"text": "форма работает на телефоне", "original": "Das Formular läuft mobil."}
    data = review_data(REVIEW_TICKET_DE)
    data["tasks"][0]["acceptance"].append(invented)

    stamped = Review.model_validate_json(
        stamp_review(json.dumps(data, ensure_ascii=False), TICKET_DE, "ru")
    )

    marks = [criterion.in_transcript for criterion in stamped.tasks[0].acceptance]
    assert marks == [True, True, True, False]
    assert unmatched_originals(stamped, TICKET_DE) == ["Das Formular läuft mobil."]


def test_a_review_without_a_ticket_writes_no_ticket_fields() -> None:
    """Разбор встречи остаётся тем же файлом, что до тикетов: старые review.json валидны."""
    stamped = stamp_review(REVIEW_DE, MEETING_DE, "ru")

    for field in ("ticket_key", "ticket_url", "acceptance"):
        assert field not in stamped
