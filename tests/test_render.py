import pytest

from app.models import Deferred
from app.render import (
    MAX_MESSAGE_CHARACTERS,
    NOT_FOUND_IN_MESSAGE,
    backlog_digest,
    brief_digest,
    issues_markdown,
    review_lead,
    review_markdown,
    review_messages,
    task_in_message,
)
from app.review import Quote, Review
from tests.helpers import FIXTURES, REAL_BRIEF, real_issues


def review_de() -> Review:
    return Review.model_validate_json((FIXTURES / "review_de.json").read_text(encoding="utf-8"))


def test_the_rendered_backlog_matches_the_snapshot() -> None:
    expected = (FIXTURES / "issues_real.md").read_text(encoding="utf-8")

    assert issues_markdown(real_issues()) == expected


def test_an_issue_without_dependencies_shows_a_dash() -> None:
    rendered = issues_markdown(real_issues())

    assert "**Depends on:** —" in rendered


def test_a_reason_with_a_pipe_does_not_break_the_table() -> None:
    issues_file = real_issues()
    issues_file.deferred = [Deferred(scope_id="S7", title="Экспорт", reason="csv | json")]

    lines = issues_markdown(issues_file).splitlines()
    row = next(line for line in lines if line.startswith("| S7"))

    assert row == r"| S7 | Экспорт | csv \| json |"


def test_sections_without_content_are_not_printed() -> None:
    issues_file = real_issues()
    issues_file.deferred = []
    issues_file.open_questions = []

    rendered = issues_markdown(issues_file)

    assert "Deferred" not in rendered
    assert "Open questions" not in rendered


def test_every_issue_of_the_answer_reaches_the_page() -> None:
    issues_file = real_issues()

    rendered = issues_markdown(issues_file)

    assert all(f"### {issue.id} · {issue.title}" in rendered for issue in issues_file.issues)


def test_an_issue_is_printed_under_its_own_phase() -> None:
    issues_file = real_issues()
    second_phase = issues_file.phases[1]
    of_second = [issue for issue in issues_file.issues if issue.phase == second_phase.n]

    head, tail = issues_markdown(issues_file).split(f"## Phase {second_phase.n} — ")

    assert all(issue.id not in head for issue in of_second)
    assert all(issue.id in tail for issue in of_second)


def test_the_file_ends_with_a_single_newline() -> None:
    rendered = issues_markdown(real_issues())

    assert rendered.endswith("\n")
    assert not rendered.endswith("\n\n")


def test_the_brief_digest_says_what_the_frontmatter_says() -> None:
    """Пересказ брифа своими словами был бы выдуманными данными: показываем объявленное."""
    digest = brief_digest(REAL_BRIEF)

    assert digest == "Бриф готов: Бот для анбординга новичков\nОткрытых вопросов: 15."


def test_a_brief_without_frontmatter_still_says_that_it_is_ready() -> None:
    """Frontmatter пишет модель, и заглушка вместо имени соврала бы про артефакт."""
    assert brief_digest("# Бриф\n") == "Бриф готов."


def test_the_backlog_digest_counts_the_issues_of_every_phase() -> None:
    issues_file = real_issues()
    first, second = issues_file.phases

    digest = backlog_digest(issues_file)

    assert digest.startswith(
        f"Бэклог готов: {len(issues_file.issues)} задач, фаз {len(issues_file.phases)}."
    )
    for phase in (first, second):
        here = sum(1 for issue in issues_file.issues if issue.phase == phase.n)
        assert f"{phase.n}. {phase.title} — {here}" in digest


def test_the_backlog_digest_counts_the_deferred_scopes_when_there_are_any() -> None:
    issues_file = real_issues()
    issues_file.deferred = [Deferred(scope_id="S7", title="Экспорт", reason="позже")]

    assert "Отложено скоупов: 1." in backlog_digest(issues_file)


def test_the_rendered_review_matches_the_snapshot() -> None:
    expected = (FIXTURES / "review_de.md").read_text(encoding="utf-8")

    assert review_markdown(review_de()) == expected


def test_the_summary_follows_the_task_title_in_the_file() -> None:
    review = review_de()
    lines = review_markdown(review).splitlines()

    for number, task in enumerate(review.tasks, start=1):
        title = lines.index(f"## {number}. {task.title}")
        assert lines[title + 1] == task.summary


def test_only_the_unconfirmed_quote_is_marked_in_the_file() -> None:
    rendered = review_markdown(review_de())

    assert rendered.count("*(not found verbatim in transcript)*") == 1
    assert (
        "> Die Fehlermeldungen auf der Kontoseite sind noch auf Englisch. "
        "*(not found verbatim in transcript)*"
    ) in rendered


def test_every_task_gets_its_own_message_with_the_summary_second() -> None:
    review = review_de()

    messages = review_messages(review)

    assert len(messages) == len(review.tasks)
    for number, (message, task) in enumerate(zip(messages, review.tasks, strict=True), start=1):
        assert message.splitlines()[:2] == [f"{number}. {task.title}", task.summary]


@pytest.mark.parametrize(("in_transcript", "marked"), [(False, True), (None, True), (True, False)])
def test_a_quote_is_marked_in_the_message_unless_the_check_confirmed_it(
    in_transcript: bool | None, marked: bool
) -> None:
    """Файл, поправленный руками без пометки, не выдаёт фрагмент за дословный."""
    review = review_de()
    review.tasks = review.tasks[:1]
    review.tasks[0].quotes = [Quote(original="bis Freitag", in_transcript=in_transcript)]

    lines = review_messages(review)[0].splitlines()

    assert (NOT_FOUND_IN_MESSAGE in lines) is marked


def test_a_long_task_is_split_between_lines_and_no_part_is_over_the_limit() -> None:
    review = review_de()
    review.tasks = review.tasks[:1]
    long_quote = "Kannst du das bis Freitag machen? " * 80
    review.tasks[0].quotes = [
        Quote(original=long_quote.strip(), translation="перевод " * 300, in_transcript=True)
        for _ in range(3)
    ]

    messages = review_messages(review)

    assert len(messages) > 1
    assert all(len(message) <= MAX_MESSAGE_CHARACTERS for message in messages)
    assert "\n".join(messages) == "\n".join(task_in_message(1, review.tasks[0]))


def test_a_single_line_over_the_limit_is_cut_without_losing_text() -> None:
    """Whisper отдаёт расшифровку одним абзацем, и цитата из неё может не знать переносов строк."""
    review = review_de()
    review.tasks = review.tasks[:1]
    review.tasks[0].summary = "Суть. " * 1000

    messages = review_messages(review)

    assert all(len(message) <= MAX_MESSAGE_CHARACTERS for message in messages)
    assert "".join(messages).count("Суть.") == 1000


def test_a_quote_without_translation_has_no_arrow_line() -> None:
    review = review_de()
    review.tasks = review.tasks[:1]
    review.tasks[0].quotes = [Quote(original="bis Freitag", in_transcript=True)]
    review.tasks[0].ask_back = []

    lines = review_messages(review)[0].splitlines()

    assert not any(line.lstrip().startswith("→") for line in lines)


def test_the_lead_lists_the_tasks_in_meeting_order() -> None:
    review = review_de()

    assert review_lead(review).splitlines() == [
        "Разбор встречи: 2 поручения",
        "",
        *(f"{number}. {task.title}" for number, task in enumerate(review.tasks, start=1)),
    ]


def test_the_lead_of_a_meeting_without_tasks_names_its_topics() -> None:
    review = Review.model_validate_json(
        (FIXTURES / "review_none.json").read_text(encoding="utf-8")
    )

    lead = review_lead(review)

    assert lead.startswith("Разбор встречи: поручений нет.")
    assert all(f"• {topic}" in lead for topic in review.topics)
    assert review_messages(review) == []
