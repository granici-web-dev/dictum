from app.models import Deferred
from app.render import backlog_digest, brief_digest, issues_markdown
from tests.helpers import FIXTURES, REAL_BRIEF, real_issues


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
