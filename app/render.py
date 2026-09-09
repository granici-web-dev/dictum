"""outputs/issues.md — тот же бэклог для чтения человеком на воротах. См. SPEC.md §7.

Раньше его писала модель вторым файлом: половина выхода стадии уходила на копию, потолок
токенов срезал ответ, и две копии могли разойтись, не поспорив об этом вслух.

Подписи английские при любом lang: содержимое приходит на языке прогона, и русские заголовки
вокруг немецкого текста читались как ошибка. Английские нейтральны ко всем трём языкам, которые
нам встречались, и не заводят словарь ради одного файла.
"""

import frontmatter

from app.models import Issue, IssuesFile

NOTHING = "—"


def cell(text: str) -> str:
    # Причина скоупа — свободный текст модели, и вертикальная черта в нём разорвала бы строку
    # таблицы на лишние колонки.
    return text.replace("|", r"\|")


def issue_lines(issue: Issue) -> list[str]:
    depends_on = ", ".join(issue.depends_on) or NOTHING
    return [
        f"### {issue.id} · {issue.title}",
        f"- **Scope:** {issue.scope_id} · **Area:** {issue.area} "
        f"· **Estimate:** {issue.estimate} · **Depends on:** {depends_on}",
        f"- {issue.description}",
        "- DoD:",
        *(f"  - [ ] {item}" for item in issue.dod),
        f"- Check: {issue.test_hint}",
    ]


def issues_markdown(issues_file: IssuesFile) -> str:
    lines = [
        "# Backlog",
        "",
        f"Source: `{issues_file.source}`. Draft to edit before publishing: once approved, "
        "`/publish` takes `outputs/issues.json`.",
    ]
    for phase in issues_file.phases:
        lines += ["", f"## Phase {phase.n} — {phase.title}", "", f"Goal: {phase.goal}"]
        for issue in issues_file.issues:
            if issue.phase == phase.n:
                lines += ["", *issue_lines(issue)]
    if issues_file.deferred:
        lines += ["", "## Deferred", "", "| Scope | Title | Reason |", "|---|---|---|"]
        lines += [
            f"| {d.scope_id} | {cell(d.title)} | {cell(d.reason)} |" for d in issues_file.deferred
        ]
    if issues_file.open_questions:
        lines += ["", "## Open questions", ""]
        lines += [f"- {question}" for question in issues_file.open_questions]
    return "\n".join(lines) + "\n"


def brief_digest(content: str) -> str:
    """Бриф на воротах в двух строках: только то, что стадия объявила во frontmatter.

    Пересказывать бриф своими словами нечем — это выдуманные данные (CLAUDE.md §5), — а
    целиком он уходит человеку файлом. Поля необязательны: frontmatter пишет модель, и
    отсутствующее имя лучше показать пропуском, чем заглушкой.
    """
    written = frontmatter.loads(content).metadata
    lines = [f"Бриф готов: {written['name']}" if "name" in written else "Бриф готов."]
    if "open_questions" in written:
        lines.append(f"Открытых вопросов: {written['open_questions']}.")
    return "\n".join(lines)


def backlog_digest(issues_file: IssuesFile) -> str:
    """Бэклог на воротах числами: сколько задач, по каким фазам, сколько отложено."""
    lines = [
        f"Бэклог готов: {len(issues_file.issues)} задач, фаз {len(issues_file.phases)}.",
        "",
    ]
    for phase in issues_file.phases:
        here = sum(1 for issue in issues_file.issues if issue.phase == phase.n)
        lines.append(f"{phase.n}. {phase.title} — {here}")
    if issues_file.deferred:
        lines += ["", f"Отложено скоупов: {len(issues_file.deferred)}."]
    return "\n".join(lines)
