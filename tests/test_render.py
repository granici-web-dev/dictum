from app.models import Deferred
from app.render import issues_markdown
from tests.helpers import FIXTURES, real_issues


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
