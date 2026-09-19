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

from app.answers import Answers
from app.approach import (
    Approach,
    KnownFrom,
    Mode,
    Option,
    Recommendation,
    confirmed_source_ids,
    outside_the_standards,
)
from app.clarify import Clarify
from app.models import Issue, IssuesFile
from app.project import STANDARD_ALIASES, Project
from app.review import AskBack, Quote, Review, Said, Task
from app.steps import AskedQuestion, Pair, Steps

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


def minutes_count(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        word = "минута"
    elif 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        word = "минуты"
    else:
        word = "минут"
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


ANSWERS_IN_FILE = {
    "without_answers": "went on without answers",
    "not_sent": "questions were not sent",
    "nothing_asked": "nothing to ask",
}


def answers_in_file(answers: Answers) -> str:
    if answers.received_at is None:
        return ANSWERS_IN_FILE[answers.status]
    return f"received {answers.received_at:%Y-%m-%d %H:%M} UTC"


def standards_in_file(project: Project) -> str:
    found = [name for name in STANDARD_ALIASES if name in project.texts]
    if not found:
        return f"none found in `{project.source}`" if project.configured else "not set"
    return f"`{project.source}` ({', '.join(found)})"


def asked_in_file(question: AskedQuestion) -> list[str]:
    mark = "" if question.answered else " *(no answer)*"
    lines = [f"{question.number}. {question.text}{mark}"]
    if question.translation:
        lines.append(f"   → {question.translation}")
    return lines


def steps_markdown(steps: Steps, answers: Answers, project: Project) -> str:
    """Шаги поручения для чтения на воротах, со сказанным на встрече и вопросами для тимлида."""
    lines = [*pair_in_file(steps.title, "# ", ""), "", *pair_in_file(steps.summary, "", "")]
    task = steps.task
    if task:
        deadline = said_in_file(task.deadline) if task.deadline else NOTHING
        lines += [
            "",
            f"- **From review:** `{task.parent_run_id}`, task {task.number}",
            f"- **Deadline:** {deadline}",
            f"- **Teamlead answers:** {answers_in_file(answers)}",
            f"- **Project standards:** {standards_in_file(project)}",
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
    if steps.questions:
        lines += ["", "## Questions for the teamlead", ""]
        for asked in steps.questions:
            lines += asked_in_file(asked)
    if answers.text:
        quoted = [f"> {line}" for line in answers.text.splitlines()]
        lines += ["", "## Teamlead answers", "", *quoted]
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


# Последствие незаданных стандартов называется до того, как за него заплачено: забытая настройка
# иначе оплачивает подбор стека проекту, у которого стек давно есть.
MISSING_STANDARDS_CONSEQUENCE = "ресёрч будет подбирать стек как для нового проекта"


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


# Разделов в тексте для копирования нет: заголовок на языке встречи потребовал бы словаря на
# каждый язык, а от словаря проект отказался. Разделы различают первая строка, номера у шагов и
# эти две пометки.
UNANSWERED_MARK = "❓"
APPROACH_MARK = "→"


def steps_copy_text(steps: Steps) -> str:
    """Шаги на языке встречи одним куском: владелец копирует его и показывает тимлиду сам.

    Ни подписей бота, ни фраз-обращений: текст пишется владельцу, а кому и в каком виде его
    показать, решает он. Вопросы самому владельцу (`open_questions`) сюда не идут — тимлиду они
    не адресованы.
    """
    ticket = steps.task.ticket_key if steps.task else None
    blocks = [
        " ".join(filter(None, (ticket, steps.title.text))),
        steps.summary.text,
        "\n".join(f"{number}. {step.text}" for number, step in enumerate(steps.steps, start=1)),
    ]
    blocks += [
        f"{UNANSWERED_MARK} {asked.text}" for asked in steps.questions if not asked.answered
    ]
    if steps.approach:
        blocks.append(f"{APPROACH_MARK} {steps.approach.text}")
    return "\n\n".join(blocks)


RESEARCH_MODE_TEXT: dict[Mode, str] = {
    "existing": "внутри стека проекта",
    "new": "новый проект",
}

RESEARCH_SKIPPED_TEXT = "Ресёрч пропущен."


def sources_count(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        word = "источник"
    elif 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        word = "источника"
    else:
        word = "источников"
    return f"{count} {word}"


def research_line(approach: Approach) -> str:
    """Ресёрч на воротах одной строкой: режим, выбор и сколько ссылок правда пришло из поиска."""
    if approach.recommendation is None:
        return RESEARCH_SKIPPED_TEXT
    mode = (
        "предварительно, стек не подтверждён"
        if approach.recommendation.provisional
        else RESEARCH_MODE_TEXT[approach.mode or "new"]
    )
    found = len(confirmed_source_ids(approach))
    return (
        f"Ресёрч: {mode}, {approach.recommendation.option}, "
        f"{sources_count(len(approach.sources))}, из поиска {found}."
    )


SNAPSHOT_TAKEN_AT = "%d.%m %H:%M UTC"


def standards_snapshot_line(project: Project) -> str:
    """Каталог стандартов перестал читаться: шаги написаны по снимку, и по какому — видно."""
    return (
        f"Стандарты проекта: снимок от {project.taken_at:{SNAPSHOT_TAKEN_AT}}, "
        "каталог сейчас недоступен"
    )


# Пометки ресёрча (SPEC §7): вывод без подтверждённого источника или вне стандартов проекта не
# должен выглядеть проверенным. Английские, как все подписи в файлах: содержимое приходит на
# языке владельца, а заголовки вокруг него фиксированы.
OUTSIDE_STANDARDS = "*(outside the project standards)*"
NEW_DEPENDENCY = "*(new dependency: the teamlead decides)*"
QUOTE_NOT_IN_STANDARDS = "*(quote not found in the standards)*"
PROVISIONAL = "*(provisional: the stack is not confirmed)*"
NOT_FROM_SEARCH = "*(not in the search results)*"
NO_CONFIRMED_SOURCE = "*(no source: the model's opinion)*"

MODE_IN_FILE: dict[Mode, str] = {
    "existing": "existing project, inside its stack",
    "new": "new project, the stack is being chosen",
}

KNOWN_FROM_IN_FILE: dict[KnownFrom, str] = {
    "standards": "standards",
    "task": "task",
    "answers": "teamlead answers",
}


def sources_in_file(named: list[str], confirmed: set[str]) -> str:
    """Источники места. Ни одного подтверждённого поиском — это мнение модели, и так и написано."""
    if not named:
        return NO_CONFIRMED_SOURCE
    listed = ", ".join(named)
    return listed if set(named) & confirmed else f"{listed} {NO_CONFIRMED_SOURCE}"


def option_in_file(
    number: int, option: Option, confirmed: set[str], outside: bool
) -> list[str]:
    lines = [f"### {number}. {option.name}{' ' + OUTSIDE_STANDARDS if outside else ''}", ""]
    lines += [option.summary, ""]
    if option.uses:
        lines.append(f"- **Uses:** {', '.join(option.uses)}")
    if option.adds:
        why = f" — {option.adds_why}" if option.adds_why else ""
        lines.append(f"- **Adds:** {', '.join(option.adds)}{why} {NEW_DEPENDENCY}")
    for heading, items in (("Pros", option.pros), ("Cons", option.cons), ("Risks", option.risks)):
        if items:
            lines.append(f"- **{heading}:** {'; '.join(items)}")
    return lines + [
        f"- **Cost:** {option.cost}",
        f"- **Sources:** {sources_in_file(option.sources, confirmed)}",
    ]


def recommendation_in_file(recommendation: Recommendation, confirmed: set[str]) -> list[str]:
    mark = f" {PROVISIONAL}" if recommendation.provisional else ""
    lines = [
        "## Recommendation",
        "",
        f"**{recommendation.option}**{mark}",
        "",
        recommendation.why,
        "",
        f"- **Sources:** {sources_in_file(recommendation.sources, confirmed)}",
    ]
    if recommendation.how_to_write:
        lines += ["", "### How to write it", ""]
        lines += [f"{number}. {item}" for number, item in enumerate(recommendation.how_to_write, 1)]
    if recommendation.standards_refs:
        lines += ["", "### Project standards", ""]
        for reference in recommendation.standards_refs:
            missing = "" if reference.in_snapshot else f" {QUOTE_NOT_IN_STANDARDS}"
            lines += [f"- `{reference.file}`{missing}", f"  > {reference.quote}"]
    return lines


def approach_markdown(approach: Approach, project: Project, assignment_and_answers: str) -> str:
    """Ресёрч для чтения на воротах шагов: варианты, рекомендация и честность её источников.

    Пометку «вне стандартов проекта» код считает заново по снимку, а не хранит полем: после
    ремонта вариант остаётся в файле, и о том, что он вышел за стек, человек узнаёт здесь.
    """
    confirmed = confirmed_source_ids(approach)
    lines = [
        "# Approach",
        "",
        f"- **Mode:** {MODE_IN_FILE[approach.mode]}" if approach.mode else "- **Mode:** —",
        f"- **Project standards:** {standards_in_file(project)}",
    ]
    if approach.stack_quote:
        found = "" if approach.stack_named else f" {QUOTE_NOT_IN_STANDARDS}"
        lines.append(f"- **Stack named in the task:** {approach.stack_quote}{found}")
    if approach.known:
        lines += ["", "## Known", ""]
        lines += [
            f"- {item.text} ({KNOWN_FROM_IN_FILE[item.origin]})" for item in approach.known
        ]
    if approach.unknown:
        lines += ["", "## Unknown", ""]
        lines += [f"- {item}" for item in approach.unknown]
    if approach.rejected:
        lines += ["", "## Rejected in the standards", ""]
        for rejected in approach.rejected:
            lines += [f"- **{rejected.name}**", f"  > {rejected.quote}"]
    if approach.options:
        lines += ["", "## Options"]
        for number, option in enumerate(approach.options, start=1):
            # У нового проекта стандартов нет, и выйти за них нечему: пометка была бы ложью.
            outside = approach.mode == "existing" and outside_the_standards(
                option, project, assignment_and_answers
            )
            lines += ["", *option_in_file(number, option, confirmed, outside)]
    if approach.recommendation:
        lines += ["", *recommendation_in_file(approach.recommendation, confirmed)]
    if approach.new_questions:
        lines += ["", "## New questions for the teamlead", ""]
        for question in approach.new_questions:
            lines.append(f"- {question.text}")
            if question.translation:
                lines.append(f"  → {question.translation}")
            lines.append(f"  Why: {question.why}")
    if approach.sources:
        lines += ["", "## Sources", ""]
        for source in approach.sources:
            invented = "" if source.found_by_search else f" {NOT_FROM_SEARCH}"
            lines.append(f"- **{source.id}** {source.title} — {source.url}{invented}")
    if approach.searches:
        lines += ["", "## Searches", ""]
        lines += [f"- {query}" for query in approach.searches]
    return "\n".join(lines) + "\n"


# Задание для `/rigorous shape` в рабочем репозитории (P3-11, часть F). Команду владелец
# набирает сам, поэтому её слов в файле нет: файл — это то, что он вставляет следом.
SHAPE_PROMPT_LEAD = "Plan this task with the project's standards before writing any code."
NO_TEAMLEAD_ANSWERS = "no answers"
# Правка на воротах («делаем на Formik») меняет шаги, а approach.json остаётся прежним. Код
# видит это без модели: строка подхода в шагах разошлась с рекомендацией ресёрча.
RESEARCH_CHANGED = (
    "Approach was changed at approval: the research below compared options for a different choice."
)
OPEN_QUESTIONS_LEAD = "The teamlead has not answered these. Do not assume an answer: ask."


def research_in_prompt(approach: Approach, chosen: Pair | None) -> list[str]:
    """Ресёрч в задании: выбранный подход, почему он, как это писать и чем это подтверждено."""
    recommendation = approach.recommendation
    if recommendation is None:
        return []
    headline = chosen or recommendation.headline
    lines = ["## Research", "", *pair_in_file(headline, "", "")]
    if chosen is not None and chosen.text != recommendation.headline.text:
        lines += ["", RESEARCH_CHANGED]
    else:
        lines += ["", recommendation.why]
        if recommendation.how_to_write:
            lines += ["", "### How to write it", ""]
            lines += [
                f"{number}. {item}"
                for number, item in enumerate(recommendation.how_to_write, start=1)
            ]
        if recommendation.standards_refs:
            lines += ["", "### Project standards", ""]
            for reference in recommendation.standards_refs:
                missing = "" if reference.in_snapshot else f" {QUOTE_NOT_IN_STANDARDS}"
                lines += [f"- `{reference.file}`{missing}", f"  > {reference.quote}"]
    # Только подтверждённые поиском: непроверенная ссылка в задании выглядела бы источником.
    found = [source for source in approach.sources if source.found_by_search]
    if found:
        lines += ["", "### Sources", ""]
        lines += [f"- {source.title} — {source.url}" for source in found]
    return lines


def shape_prompt(steps: Steps, answers: Answers, approach: Approach) -> str:
    """Задание для `/rigorous shape`, собранное кодом из проверенных артефактов прогона.

    Модель его не пересказывает: всё, что здесь стоит, уже проверено и проштамповано своей
    стадией, а пересказ разошёлся бы с артефактами, не поспорив об этом вслух (SPEC §7.3).
    """
    lines = [SHAPE_PROMPT_LEAD, "", "## Task", "", *pair_in_file(steps.title, "", "")]
    lines += ["", *pair_in_file(steps.summary, "", "")]
    task = steps.task
    if task and task.ticket_key:
        ticket = " — ".join(filter(None, (task.ticket_key, task.ticket_url)))
        lines += ["", f"- **Ticket:** {ticket}"]
    if task and task.deadline:
        lines += ["", "## Deadline", "", said_in_file(task.deadline)]
    if task:
        for heading, said in (
            ("Constraints", task.constraints),
            ("Acceptance criteria", task.acceptance),
            ("Do not", task.do_not),
        ):
            if said:
                lines += ["", f"## {heading}", "", *(f"- {said_in_file(item)}" for item in said)]
    lines += ["", "## Teamlead answers", ""]
    if answers.text:
        lines += [f"> {line}" for line in answers.text.splitlines()]
    else:
        lines.append(NO_TEAMLEAD_ANSWERS)
    research = research_in_prompt(approach, steps.approach)
    if research:
        lines += ["", *research]
    lines += ["", "## Steps", ""]
    for number, step in enumerate(steps.steps, start=1):
        lines += pair_in_file(step, f"{number}. ", "   ")
    unanswered = [asked for asked in steps.questions if not asked.answered]
    if unanswered:
        lines += ["", "## Open questions", "", OPEN_QUESTIONS_LEAD, ""]
        for asked in unanswered:
            lines.append(f"{asked.number}. {asked.text}")
            if asked.translation:
                lines.append(f"   → {asked.translation}")
    return "\n".join(lines) + "\n"
