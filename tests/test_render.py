from app.models import Deferred
from app.render import candidate_titles, issues_markdown
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


# Слово в слово из прогона d090c91a0a1b4bc4: две несвязанные просьбы одной записью.
REAL_CANDIDATES = """# В записи найдено 2 идеи

1. **Бот для онбординга новичков** — пошаговый бот с чек-листом (что поставить, где взять \
доступы, к кому обращаться) для новых сотрудников. Сказал: unknown. Статус: решение.
2. **Утренняя сводка по просроченным дедлайнам** — ежедневное сообщение со списком карточек \
Trello, у которых дедлайн сегодня или уже прошёл. Сказал: unknown. Статус: решение.

Выберите номер — или «все», тогда каждая пойдёт отдельным прогоном.
"""


def test_the_candidate_list_keeps_the_numbers_and_drops_the_bookkeeping() -> None:
    """Человек в чате выбирает по названию: кто сказал и какой статус — не его забота."""
    assert candidate_titles(REAL_CANDIDATES) == [
        "1. Бот для онбординга новичков",
        "2. Утренняя сводка по просроченным дедлайнам",
    ]


def test_a_line_without_a_bold_name_is_shown_whole() -> None:
    """Формат — выход модели: если жирного нет, лучше показать строку, чем потерять идею."""
    assert candidate_titles("1. Просто идея без выделения") == ["1. Просто идея без выделения"]


def test_prose_without_a_numbered_list_yields_nothing() -> None:
    assert candidate_titles("# Идея не найдена\n\nВ записи только обсуждение.") == []
