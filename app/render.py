"""outputs/issues.md — тот же бэклог для чтения человеком на воротах. См. SPEC.md §7.

Здесь же разбор встречи из review.json: outputs/review.md и сообщения бота (P3-08).

Раньше его писала модель вторым файлом: половина выхода стадии уходила на копию, потолок
токенов срезал ответ, и две копии могли разойтись, не поспорив об этом вслух.

Подписи английские при любом lang: содержимое приходит на языке прогона, и русские заголовки
вокруг немецкого текста читались как ошибка. Английские нейтральны ко всем трём языкам, которые
нам встречались, и не заводят словарь ради одного файла.
"""

import frontmatter

from app.models import Issue, IssuesFile
from app.review import AskBack, Quote, Review, Said, Task

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


# Предел сообщения Bot API. Длинное поручение делится, а не обрезается: срезанным оказалось бы как
# раз «не надо» или «переспросить», ради которых разбор и затевался.
MAX_MESSAGE_CHARACTERS = 4096

STATUS_LABEL = {"decision": "decision", "thinking_aloud": "thinking aloud"}
STATUS_TEXT = {"decision": "решение", "thinking_aloud": "мысль вслух"}

NOT_FOUND_IN_FILE = "*(not found verbatim in transcript)*"
NOT_FOUND_IN_MESSAGE = "(в расшифровке дословно не найдено)"


def confirmed(fragment: Said | Quote) -> bool:
    # Файл, поправленный руками без пометки, читается как неподтверждённый, а не как дословный.
    return fragment.in_transcript is True


def said_in_file(said: Said) -> str:
    line = f"{said.text} (*{said.original}*)"
    return line if confirmed(said) else f"{line} {NOT_FOUND_IN_FILE}"


def ask_back_in_file(ask: AskBack) -> list[str]:
    lines = [f"- {ask.text}", f"  > {ask.question}"]
    if ask.translation:
        lines += ["  >", f"  > → {ask.translation}"]
    return lines


def quote_in_file(quote: Quote) -> list[str]:
    original = quote.original if confirmed(quote) else f"{quote.original} {NOT_FOUND_IN_FILE}"
    lines = [f"> {original}"]
    if quote.translation:
        lines += [">", f"> → {quote.translation}"]
    return lines


def task_in_file(number: int, task: Task) -> list[str]:
    deadline = said_in_file(task.deadline) if task.deadline else NOTHING
    lines = [
        f"## {number}. {task.title}",
        task.summary,
        "",
        f"- **Assigned by:** {task.assigned_by}",
        f"- **Assignee:** {task.assignee}",
        f"- **Status:** {STATUS_LABEL[task.status]}",
        f"- **Deadline:** {deadline}",
    ]
    for heading, said in (("Constraints", task.constraints), ("Do not", task.do_not)):
        if said:
            lines += ["", f"### {heading}", *(f"- {said_in_file(item)}" for item in said)]
    if task.ask_back:
        lines += ["", "### Ask back"]
        for ask in task.ask_back:
            lines += ask_back_in_file(ask)
    lines += ["", "### Quotes"]
    for index, quote in enumerate(task.quotes):
        lines += ([""] if index else []) + quote_in_file(quote)
    return lines


def review_markdown(review: Review) -> str:
    lines = ["# Meeting review"]
    if not review.tasks:
        lines += ["", "No tasks in this meeting."]
        if review.topics:
            lines += ["", "## Topics", "", *(f"- {topic}" for topic in review.topics)]
    for number, task in enumerate(review.tasks, start=1):
        lines += ["", *task_in_file(number, task)]
    return "\n".join(lines) + "\n"


def tasks_count(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        word = "поручение"
    elif 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        word = "поручения"
    else:
        word = "поручений"
    return f"{count} {word}"


def review_lead(review: Review) -> str:
    """Оглавление разбора: оно встаёт в сообщение о ходе прогона вместо галочек."""
    if not review.tasks:
        lines = ["Разбор встречи: поручений нет."]
        if review.topics:
            lines += ["", "О чём говорили:", *(f"• {topic}" for topic in review.topics)]
        return "\n".join(lines)
    return "\n".join(
        [
            f"Разбор встречи: {tasks_count(len(review.tasks))}",
            "",
            *(f"{number}. {task.title}" for number, task in enumerate(review.tasks, start=1)),
        ]
    )


def said_in_message(said: Said) -> list[str]:
    lines = [f"• {said.text}", f"  «{said.original}»"]
    return lines if confirmed(said) else [*lines, f"  {NOT_FOUND_IN_MESSAGE}"]


def task_in_message(number: int, task: Task) -> list[str]:
    lines = [
        f"{number}. {task.title}",
        task.summary,
        f"Поручил: {task.assigned_by} · Кому: {task.assignee}",
        f"Статус: {STATUS_TEXT[task.status]}",
    ]
    if task.deadline:
        lines += [f"Срок: {task.deadline.text}", f"  «{task.deadline.original}»"]
        if not confirmed(task.deadline):
            lines.append(f"  {NOT_FOUND_IN_MESSAGE}")
    else:
        lines.append("Срок: не назван")
    for heading, said in (("Ограничения:", task.constraints), ("Не надо:", task.do_not)):
        if said:
            lines.append(heading)
            for item in said:
                lines += said_in_message(item)
    if task.ask_back:
        lines.append("Переспросить:")
        for ask in task.ask_back:
            lines += [f"• {ask.text}", f"  «{ask.question}»"]
            if ask.translation:
                lines.append(f"  → «{ask.translation}»")
    lines.append("Цитаты:")
    for quote in task.quotes:
        lines.append(f"«{quote.original}»")
        if not confirmed(quote):
            lines.append(NOT_FOUND_IN_MESSAGE)
        if quote.translation:
            lines.append(f"→ «{quote.translation}»")
    return lines


def split_message(lines: list[str]) -> list[str]:
    """Части не длиннее предела, разрезанные по границам строк.

    Строку длиннее предела целиком не отправить, и режется только она: расшифровка Whisper
    приходит одним абзацем, и цитата из неё может не знать ни одного перевода строки.
    """
    pieces = [
        line[start : start + MAX_MESSAGE_CHARACTERS]
        for line in lines
        for start in range(0, max(len(line), 1), MAX_MESSAGE_CHARACTERS)
    ]
    parts: list[list[str]] = [[]]
    size = 0
    for piece in pieces:
        added = len(piece) + (1 if parts[-1] else 0)
        if parts[-1] and size + added > MAX_MESSAGE_CHARACTERS:
            parts.append([])
            added = len(piece)
            size = 0
        parts[-1].append(piece)
        size += added
    return ["\n".join(part) for part in parts]


def review_messages(review: Review) -> list[str]:
    """По сообщению на поручение, в порядке встречи; длинное поручение уходит несколькими."""
    return [
        message
        for number, task in enumerate(review.tasks, start=1)
        for message in split_message(task_in_message(number, task))
    ]
