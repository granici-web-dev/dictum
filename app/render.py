"""outputs/issues.md — тот же бэклог для чтения человеком на воротах. См. SPEC.md §7.

Здесь же разбор встречи из review.json: outputs/review.md и сообщения бота (P3-08), вопросы для
тимлида из clarify.json двумя сообщениями (P3-11) и шаги поручения из steps.json:
outputs/steps.md для ворот.

Раньше его писала модель вторым файлом: половина выхода стадии уходила на копию, потолок
токенов срезал ответ, и две копии могли разойтись, не поспорив об этом вслух.

Подписи английские при любом lang: содержимое приходит на языке прогона, и русские заголовки
вокруг немецкого текста читались как ошибка. Английские нейтральны ко всем трём языкам, которые
нам встречались, и не заводят словарь ради одного файла.
"""

import frontmatter

from app.clarify import Clarify
from app.models import Issue, IssuesFile
from app.project import STANDARD_ALIASES, Project
from app.review import AskBack, Quote, Review, Said, Task
from app.steps import Pair, Steps

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
    ]
    ticket = " · ".join(filter(None, (task.ticket_key, task.ticket_url)))
    if ticket:
        lines.append(f"- **Ticket:** {ticket}")
    lines.append(f"- **Deadline:** {deadline}")
    for heading, said in (
        ("Constraints", task.constraints),
        ("Acceptance criteria", task.acceptance),
        ("Do not", task.do_not),
    ):
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
    if task.ticket_key:
        lines.append(f"Тикет: {task.ticket_key}")
    if task.ticket_url:
        lines.append(task.ticket_url if task.ticket_key else f"Тикет: {task.ticket_url}")
    if task.deadline:
        lines += [f"Срок: {task.deadline.text}", f"  «{task.deadline.original}»"]
        if not confirmed(task.deadline):
            lines.append(f"  {NOT_FOUND_IN_MESSAGE}")
    else:
        lines.append("Срок: не назван")
    for heading, said in (
        ("Ограничения:", task.constraints),
        ("Критерии приёмки:", task.acceptance),
        ("Не надо:", task.do_not),
    ):
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


def review_messages(review: Review) -> list[list[str]]:
    """Части сообщения каждого поручения, в порядке встречи; длинное поручение делится на части.

    Части сгруппированы по поручению: кнопку поручения вешают на его последнюю часть.
    """
    return [
        split_message(task_in_message(number, task))
        for number, task in enumerate(review.tasks, start=1)
    ]


def pair_in_file(pair: Pair, lead: str, indent: str) -> list[str]:
    """Текст на языке встречи и перевод стрелкой строкой ниже, как цитата в review.md."""
    lines = [f"{lead}{pair.text}"]
    if pair.translation:
        lines.append(f"{indent}→ {pair.translation}")
    return lines


def steps_markdown(steps: Steps) -> str:
    """Шаги поручения для чтения на воротах, вместе со сказанным на встрече о поручении."""
    lines = [*pair_in_file(steps.title, "# ", ""), "", *pair_in_file(steps.summary, "", "")]
    task = steps.task
    if task:
        deadline = said_in_file(task.deadline) if task.deadline else NOTHING
        lines += [
            "",
            f"- **From review:** `{task.parent_run_id}`, task {task.number}",
            f"- **Deadline:** {deadline}",
        ]
        for heading, said in (("Constraints", task.constraints), ("Do not", task.do_not)):
            if said:
                lines += ["", f"## {heading}", "", *(f"- {said_in_file(item)}" for item in said)]
    lines += ["", "## Steps", ""]
    for number, step in enumerate(steps.steps, start=1):
        lines += pair_in_file(step, f"{number}. ", "   ")
    if steps.open_questions:
        lines += ["", "## Open questions", ""]
        for question in steps.open_questions:
            lines += pair_in_file(question, "- ", "  ")
    if task and task.ask_back:
        lines += ["", "## Ask back", ""]
        for ask in task.ask_back:
            lines += ask_back_in_file(ask)
    return "\n".join(lines) + "\n"


def steps_count(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        word = "шаг"
    elif 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        word = "шага"
    else:
        word = "шагов"
    return f"{count} {word}"


def steps_digest(steps: Steps) -> str:
    """Шаги на воротах одной строкой: номер и название поручения, сколько шагов и вопросов."""
    number = f" {steps.task.number}" if steps.task else ""
    title = steps.title.translation or steps.title.text
    return (
        f"Поручение{number} «{title}»: {steps_count(len(steps.steps))}, "
        f"открытых вопросов {len(steps.open_questions)}."
    )


def questions_count(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        word = "вопрос"
    elif 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        word = "вопроса"
    else:
        word = "вопросов"
    return f"{count} {word}"


def questions_copy_text(clarify: Clarify) -> str:
    """Текст для копирования: только вопросы на языке встречи, по абзацу на вопрос.

    Ни подписи бота, ни названия поручения: вопрос уходит тимлиду как есть. Номер ставит код, по
    нему владелец сверяет вопрос с переводом в следующем сообщении.
    """
    return "\n\n".join(
        f"{number}. {question.text}"
        for number, question in enumerate(clarify.questions, start=1)
    )


# Последствие незаданных стандартов называется до того, как за него заплачено. Пока ресёрча в
# маршруте нет, платят за него шаги.
MISSING_STANDARDS_CONSEQUENCE = "шаги пишутся без стандартов проекта"


def standards_line(project: Project) -> str:
    if not project.configured:
        return f"Стандарты проекта не заданы: {MISSING_STANDARDS_CONSEQUENCE}"
    found = [name for name in STANDARD_ALIASES if name in project.texts]
    if not found:
        return (
            f"Стандарты проекта не заданы: в каталоге {project.source} нет "
            f"{', '.join(STANDARD_ALIASES)}, {MISSING_STANDARDS_CONSEQUENCE}"
        )
    return f"Стандарты проекта: {project.source} ({', '.join(found)})"


def address_line(clarify: Clarify) -> str:
    if clarify.address == "neutral":
        return "Обращение: нейтральное."
    if clarify.address == "formal":
        found = " (Sie)" if clarify.address_in_text else " (Sie): по записи не понять"
        return f"Обращение: формальное{found}."
    if clarify.address_in_text:
        return f"Обращение: неформальное (du), в записи: «{clarify.address_quote}»."
    return "Обращение: неформальное (du): в записи не найдено, проверьте перед отправкой."


def questions_note(clarify: Clarify, number: int, project: Project) -> str:
    """Перевод, зачем спрашивать, обращение и стандарты: сообщение владельцу рядом с вопросами."""
    title = clarify.title.translation or clarify.title.text
    lines = [
        f"Поручение {number} «{title}»: {questions_count(len(clarify.questions))}. Отберите "
        "нужные и отправьте тимлиду сами, каждый понятен без соседних.",
        "",
    ]
    for index, question in enumerate(clarify.questions, start=1):
        lines += [
            f"{index}. {question.translation or question.text}",
            f"   Зачем: {question.why}",
        ]
    lines += [
        "",
        address_line(clarify),
        standards_line(project),
        "",
        "Ответ пришлите ответом (reply) на это сообщение или на вопросы выше. Можно частично: "
        "вопросы без ответа останутся открытыми на шагах и на карточке.",
    ]
    return "\n".join(lines)
