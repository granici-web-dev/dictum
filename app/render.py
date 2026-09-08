"""outputs/issues.md — тот же бэклог для чтения человеком на воротах. См. SPEC.md §7.

Раньше его писала модель вторым файлом: половина выхода стадии уходила на копию, потолок
токенов срезал ответ, и две копии могли разойтись, не поспорив об этом вслух.
"""

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
        f"- **Скоп:** {issue.scope_id} · **Область:** {issue.area} "
        f"· **Оценка:** {issue.estimate} · **Зависит от:** {depends_on}",
        f"- {issue.description}",
        "- DoD:",
        *(f"  - [ ] {item}" for item in issue.dod),
        f"- Проверка: {issue.test_hint}",
    ]


def issues_markdown(issues_file: IssuesFile) -> str:
    lines = [
        "# Бэклог",
        "",
        f"Источник: `{issues_file.source}`. Черновик для правки перед публикацией: "
        "после подтверждения `/publish` берёт `outputs/issues.json`.",
    ]
    for phase in issues_file.phases:
        lines += ["", f"## Фаза {phase.n} — {phase.title}", "", f"Цель: {phase.goal}"]
        for issue in issues_file.issues:
            if issue.phase == phase.n:
                lines += ["", *issue_lines(issue)]
    if issues_file.deferred:
        lines += ["", "## Отложено", "", "| Скоп | Название | Причина |", "|---|---|---|"]
        lines += [
            f"| {d.scope_id} | {cell(d.title)} | {cell(d.reason)} |" for d in issues_file.deferred
        ]
    if issues_file.open_questions:
        lines += ["", "## Открытые вопросы", ""]
        lines += [f"- {question}" for question in issues_file.open_questions]
    return "\n".join(lines) + "\n"
