"""publish: issues.json → списки, карточки, чеклисты DoD и лейблы Trello. См. SPEC.md §6.

python -m app.publish outputs/issues.json
python -m app.publish outputs/steps.json

Путь идеи ложится фазами в списки, а поручение одной карточкой в список Assignments, с шагами
чек-листом. Форму выбирает стадия, а у ручной команды имя файла-контракта, но не его содержимое.

Что уже опубликовано, знает доска: в description каждой карточки стоит маркер
dictum:<KEY-N> run:<run_id> local:<I-00N|S<N>|A<N>>. Рядом с файлом пишется publish.json —
кэш этого отображения для человека, не источник.
"""

import json
import logging
import re
import sys
from contextlib import closing
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel

from app.config import ConfigError, InvalidProjectKey, settings
from app.models import Area, Issue, IssuesFile, Phase
from app.review import Said
from app.steps import Pair, Steps, TaskRef, schema_problems
from app.trello import Trello, TrelloCard, TrelloChecklist, TrelloError, open_trello
from app.validate import check_issues

# Имя задано строкой, а не __name__: модуль запускают как `python -m`, и там __name__ — это
# "__main__", мимо дерева "app", которому в конце файла поднимают уровень до INFO. С __name__
# такие записи до человека не доходят.
logger = logging.getLogger("app.publish")

BACKLOG = "Backlog"
DOD = "DoD"
# Входной список поручений: отсюда владелец двигает карточки по своим колонкам, а бот в его
# колонки не пишет.
ASSIGNMENTS_LIST = "Assignments"
STEPS_CHECKLIST = "Steps"
ISSUES_FILE = "issues.json"
STEPS_FILE = "steps.json"
NOT_FOUND_ON_CARD = "(not found verbatim in transcript)"

LabelName = Area | Literal["deferred"]
DEFERRED: LabelName = "deferred"

# Маркер стоит последней строкой, но описание могло уехать в веб-редактор Trello и вернуться
# с отступами или CRLF, а строку dictum: мог процитировать и сам текст issue: берётся последняя.
MARKER = re.compile(
    r"^[ \t]*dictum:(?P<key>\S+)[ \t]+run:(?P<run>\S+)[ \t]+local:(?P<local>\S+)[ \t\r]*$",
    re.MULTILINE,
)
PROJECT_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")

Status = Literal["created", "completed", "existing", "differs"]
# Чем кончилась досборка карточки: всё было на месте, дописано недостающее или в чек-листе
# стоит пункт, которого в плане нет, и дописывать в него нельзя.
Filling = Literal["complete", "filled", "foreign"]

LABEL_COLOURS: dict[LabelName, str] = {
    "frontend": "blue",
    "backend": "green",
    "design": "purple",
    "infra": "orange",
    "research": "yellow",
    DEFERRED: "black",
}

STATUS_WORDS: dict[Status, str] = {
    "created": "создана",
    "completed": "уже есть, дособрана",
    "existing": "уже есть",
    "differs": "уже есть, отличается",
}

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 64


class MissingRunId(ValueError):
    def __init__(self, path: Path, stamped_by: str = "decompose из transcript.md") -> None:
        super().__init__(
            f"в {path} нет run_id. Его проставляет {stamped_by} (SPEC §3.1); "
            "publish своего не выдаёт, иначе у прогона было бы два разных номера. "
            "Если карточки этого прогона уже на доске, возьмите run: из маркера любой из них."
        )


class MissingTask(ValueError):
    def __init__(self, path: Path) -> None:
        super().__init__(
            f"в {path} нет task: срок, условия и «не надо» вписывает стадия steps из "
            "assignment.json, и без них карточка потеряла бы сказанное на встрече. "
            "Повторите стадию: make run-text ARGS=\"--from steps\"."
        )


class InvalidIssues(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__(f"проблем {len(problems)}")
        self.problems = problems


class InvalidSteps(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__(f"проблем {len(problems)}")
        self.problems = problems


class PublishedCard(BaseModel):
    key: str
    card_id: str
    url: str


class MarkedCard(BaseModel):
    key: str
    run_id: str
    local_id: str
    card: TrelloCard


class PlannedCard(BaseModel):
    local_id: str
    phase: int | None
    list_id: str
    title: str
    # Весь текст описания над строкой зависимостей и маркером: его форма у каждого пути своя.
    body: str
    label_ids: list[str]
    checklist_name: str
    checklist: list[str]
    depends_on: list[str]
    position: int


class CardOutcome(BaseModel):
    local_id: str
    key: str
    phase: int | None
    status: Status
    url: str


def phase_list_name(phase: Phase) -> str:
    return f"Phase {phase.n}: {phase.title}"


def project_key() -> str:
    if not PROJECT_KEY_PATTERN.match(settings.project_key):
        raise InvalidProjectKey(
            f"PROJECT_KEY={settings.project_key!r} не годится в префикс ключа. "
            "Нужны заглавные латинские буквы и цифры, от двух до десяти знаков, например DCT."
        )
    return settings.project_key


def card_name(key: str, title: str) -> str:
    return f"[{key}] {title}"


def card_description(
    key: str, run_id: str, planned: PlannedCard, published: dict[str, PublishedCard]
) -> str:
    lines = [planned.body]
    if planned.depends_on:
        lines.append(
            "Depends on: " + ", ".join(published[local].key for local in planned.depends_on)
        )
    lines.append(f"dictum:{key} run:{run_id} local:{planned.local_id}")
    return "\n".join(lines)


def marked_cards(cards: list[TrelloCard]) -> list[MarkedCard]:
    found: list[MarkedCard] = []
    for card in cards:
        markers = list(MARKER.finditer(card.desc))
        if markers:
            last = markers[-1]
            found.append(
                MarkedCard(
                    key=last["key"], run_id=last["run"], local_id=last["local"], card=card
                )
            )
    return found


def next_number(on_board: list[MarkedCard], prefix: str) -> int:
    numbered = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
    taken = [int(found[1]) for card in on_board if (found := numbered.match(card.key))]
    return max(taken, default=0) + 1


def in_dependency_order(issues: list[Issue]) -> list[Issue]:
    # Ссылка на карточку-зависимость требует, чтобы та уже существовала, а в файле зависимость
    # может стоять ниже зависящего: в реальном прогоне I-006 зависел от I-010.
    by_id = {issue.id: issue for issue in issues}
    ordered: list[Issue] = []
    seen: set[str] = set()

    def visit(issue: Issue) -> None:
        if issue.id in seen:
            return
        seen.add(issue.id)
        for dependency in issue.depends_on:
            visit(by_id[dependency])
        ordered.append(issue)

    for issue in issues:
        visit(issue)
    return ordered


def ensure_lists(board: Trello, phases: list[Phase]) -> dict[str, str]:
    by_name = {item.name: item.id for item in board.lists()}
    wanted = [BACKLOG, *(phase_list_name(phase) for phase in sorted(phases, key=lambda p: p.n))]
    for name in wanted:
        if name not in by_name:
            by_name[name] = board.create_list(name, "bottom").id
    return by_name


def ensure_labels(board: Trello, names: list[LabelName]) -> dict[str, str]:
    by_name = {item.name: item.id for item in board.labels()}
    for name in names:
        if name not in by_name:
            by_name[name] = board.create_label(name, LABEL_COLOURS[name]).id
    return by_name


def plan_cards(
    issues: IssuesFile, lists: dict[str, str], labels: dict[str, str]
) -> list[PlannedCard]:
    list_of_phase = {phase.n: lists[phase_list_name(phase)] for phase in issues.phases}
    # Позиция берётся из порядка в файле, а не из порядка публикации: публикуем по зависимостям,
    # и без этого I-010 лёг бы на доске выше I-006, что читается как сбитая нумерация.
    position_of = {issue.id: number for number, issue in enumerate(issues.issues, 1)}
    planned = [
        PlannedCard(
            local_id=issue.id,
            phase=issue.phase,
            list_id=list_of_phase[issue.phase],
            title=issue.title,
            body=f"{issue.description}\n\nscope_id: {issue.scope_id}",
            label_ids=[labels[issue.area]],
            checklist_name=DOD,
            checklist=issue.dod,
            depends_on=issue.depends_on,
            position=position_of[issue.id],
        )
        for issue in in_dependency_order(issues.issues)
    ]
    planned += [
        PlannedCard(
            local_id=entry.scope_id,
            phase=None,
            list_id=lists[BACKLOG],
            title=entry.title,
            body=f"{entry.reason}\n\nscope_id: {entry.scope_id}",
            label_ids=[labels[DEFERRED]],
            checklist_name=DOD,
            checklist=[],
            depends_on=[],
            position=number,
        )
        for number, entry in enumerate(issues.deferred, 1)
    ]
    return planned


def same_card(known: TrelloCard, name: str, description: str, planned: PlannedCard) -> bool:
    return (
        known.name.strip() == name.strip()
        and known.desc.strip() == description.strip()
        and known.list_id == planned.list_id
        and sorted(known.label_ids) == sorted(planned.label_ids)
    )


def fill_checklist(
    board: Trello, card_id: str, planned: PlannedCard, existing: TrelloChecklist | None
) -> Filling:
    """Доводит чеклист карточки до полного набора пунктов, чем бы ни кончился прошлый прогон.

    Пункт, которого в плане нет, значит, что чек-лист собран из другого файла или правлен
    человеком. Дописать к нему недостающие значило бы смешать два набора, поэтому такой чек-лист
    не трогается. Имена сравниваются обрезанными: пробелы по краям Trello мог срезать сам.
    """
    if not planned.checklist:
        return "complete"
    wanted = {item.strip() for item in planned.checklist}
    present = {item.name.strip() for item in existing.check_items} if existing else set()
    if present - wanted:
        return "foreign"
    checklist = existing or board.create_checklist(card_id, planned.checklist_name)
    missing = [item for item in planned.checklist if item.strip() not in present]
    for item in missing:
        board.add_check_item(checklist.id, item)
    return "filled" if missing else "complete"


def finish_card(
    board: Trello, known: TrelloCard, planned: PlannedCard, journal: dict[str, PublishedCard]
) -> Filling:
    """Дособирает карточку, которую оборвавшийся прогон успел создать, но не успел наполнить."""
    checklist = next(
        (item for item in known.checklists if item.name == planned.checklist_name), None
    )
    filling = fill_checklist(board, known.id, planned, checklist)
    attached = {item.name for item in known.attachments}
    for dependency in planned.depends_on:
        published = journal[dependency]
        if published.key not in attached:
            board.attach_url(known.id, published.url, published.key)
            if filling == "complete":
                filling = "filled"
    return filling


def write_log(
    path: Path, published: dict[str, PublishedCard], local_ids_of_run: list[str]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        local: published[local].model_dump()
        for local in local_ids_of_run
        if local in published
    }
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def key_number(key: str) -> int:
    return int(key.rsplit("-", 1)[1])


def revisit_card(
    board: Trello,
    known: MarkedCard,
    run_id: str,
    planned: PlannedCard,
    published: dict[str, PublishedCard],
) -> CardOutcome:
    name = card_name(known.key, planned.title)
    description = card_description(known.key, run_id, planned, published)
    status: Status = "existing" if same_card(known.card, name, description, planned) else "differs"
    filling = finish_card(board, known.card, planned, published)
    # Дособранная карточка, которая вдобавок разошлась с файлом, остаётся differs:
    # человеку важнее знать про расхождение, чем про дописанный чеклист.
    if filling == "foreign":
        status = "differs"
    elif filling == "filled" and status == "existing":
        status = "completed"
    return CardOutcome(
        local_id=planned.local_id,
        key=known.key,
        phase=planned.phase,
        status=status,
        url=known.card.url,
    )


def make_card(
    board: Trello,
    key: str,
    run_id: str,
    planned: PlannedCard,
    published: dict[str, PublishedCard],
) -> CardOutcome:
    card = board.create_card(
        planned.list_id,
        card_name(key, planned.title),
        card_description(key, run_id, planned, published),
        planned.label_ids,
        planned.position,
    )
    fill_checklist(board, card.id, planned, None)
    for dependency in planned.depends_on:
        linked = published[dependency]
        board.attach_url(card.id, linked.url, linked.key)
    published[planned.local_id] = PublishedCard(key=key, card_id=card.id, url=card.url)
    logger.info("Создана карточка %s (%s): %s", key, planned.local_id, card.url)
    return CardOutcome(
        local_id=planned.local_id, key=key, phase=planned.phase, status="created", url=card.url
    )


JOURNAL_NAME = "publish.json"


def journal_of(issues_path: Path) -> Path:
    """Журнал публикации лежит рядом с issues.json того же прогона.

    Правило записано здесь одно на всех: бот читает журнал, чтобы сосчитать карточки, и пока
    путь был написан у него отдельно, две записи могли разъехаться молча.
    """
    return issues_path.parent / JOURNAL_NAME


def said_on_card(said: Said) -> str:
    return said.original if said.in_transcript is True else f"{said.original} {NOT_FOUND_ON_CARD}"


def task_card_body(steps: Steps, task: TaskRef) -> str:
    """Описание карточки поручения над маркером: сказанное в оригинале, перевод блоком в конце.

    Перевода шагов здесь нет: он стоит парой в каждом пункте чек-листа, где по шагам и работают.
    """
    lines = [steps.summary.text]
    said: list[str] = []
    if task.deadline:
        said.append(f"Deadline: {said_on_card(task.deadline)}")
    for heading, items in (("Constraints:", task.constraints), ("Do not:", task.do_not)):
        if items:
            said += [heading, *(f"- {said_on_card(item)}" for item in items)]
    if task.ask_back:
        said += ["Ask back:", *(f"- {ask.question}" for ask in task.ask_back)]
    if steps.open_questions:
        said += ["Open questions:", *(f"- {question.text}" for question in steps.open_questions)]
    if said:
        lines += ["", *said]

    translated = [pair.translation for pair in (steps.title, steps.summary) if pair.translation]
    questions = [question.translation for question in steps.open_questions if question.translation]
    if translated or questions:
        lines += ["", f"Translation ({steps.owner_lang}):", *translated]
        if questions:
            lines += ["Open questions:", *(f"- {question}" for question in questions)]
    lines += ["", f"From review: {task.parent_run_id}, task {task.number}"]
    return "\n".join(lines)


def step_item(pair: Pair) -> str:
    """Пункт чек-листа: шаг на языке встречи и перевод через косую черту в одном пункте."""
    text = pair.text.strip()
    translation = (pair.translation or "").strip()
    return f"{text} / {translation}" if translation else text


def ensure_assignments_list(board: Trello) -> str:
    for item in board.lists():
        if item.name == ASSIGNMENTS_LIST:
            return item.id
    # Слева: это вход, из которого карточки разбирают по колонкам.
    return board.create_list(ASSIGNMENTS_LIST, "top").id


def publish_task(steps_path: Path) -> CardOutcome:
    """Поручение одной карточкой: суть и сказанное в описании, шаги чек-листом Steps."""
    text = steps_path.read_text(encoding="utf-8")
    problems = schema_problems(text)
    if problems:
        raise InvalidSteps(problems)
    steps = Steps.model_validate_json(text)
    prefix = project_key()
    if not steps.run_id:
        raise MissingRunId(steps_path, "стадия steps из assignment.json")
    if steps.task is None:
        raise MissingTask(steps_path)
    run_id = steps.run_id
    local_id = f"A{steps.task.number}"
    log_path = journal_of(steps_path)

    with closing(open_trello(settings.trello_board_id, "TRELLO_BOARD_ID")) as board:
        on_board = marked_cards(board.cards())
        known = next(
            (
                found
                for found in on_board
                if found.run_id == run_id and found.local_id == local_id
            ),
            None,
        )
        published = (
            {local_id: PublishedCard(key=known.key, card_id=known.card.id, url=known.card.url)}
            if known
            else {}
        )
        planned = PlannedCard(
            local_id=local_id,
            phase=None,
            list_id=ensure_assignments_list(board),
            title=steps.title.text.strip(),
            body=task_card_body(steps, steps.task),
            label_ids=[],
            checklist_name=STEPS_CHECKLIST,
            checklist=[step_item(step) for step in steps.steps],
            depends_on=[],
            position=1,
        )
        if known:
            outcome = revisit_card(board, known, run_id, planned, published)
        else:
            key = f"{prefix}-{next_number(on_board, prefix)}"
            outcome = make_card(board, key, run_id, planned, published)
        write_log(log_path, published, [local_id])
        return outcome


def publish(issues_path: Path) -> list[CardOutcome]:
    text = issues_path.read_text(encoding="utf-8")
    problems = check_issues(text)
    if problems:
        raise InvalidIssues(problems)
    issues = IssuesFile.model_validate_json(text)
    prefix = project_key()
    if not issues.run_id:
        raise MissingRunId(issues_path)
    run_id = issues.run_id
    log_path = journal_of(issues_path)

    with closing(open_trello(settings.trello_idea_board_id, "TRELLO_IDEA_BOARD_ID")) as board:
        on_board = marked_cards(board.cards())
        next_card_number = next_number(on_board, prefix)
        cards_of_this_run = {found.local_id: found for found in on_board if found.run_id == run_id}
        published = {
            local: PublishedCard(key=found.key, card_id=found.card.id, url=found.card.url)
            for local, found in cards_of_this_run.items()
        }

        lists = ensure_lists(board, issues.phases)
        areas: list[LabelName] = sorted({issue.area for issue in issues.issues})
        labels = ensure_labels(board, areas + ([DEFERRED] if issues.deferred else []))

        planned_cards = plan_cards(issues, lists, labels)
        local_ids_of_run = [planned.local_id for planned in planned_cards]
        outcomes: list[CardOutcome] = []
        for planned in planned_cards:
            known = cards_of_this_run.get(planned.local_id)
            if known:
                outcomes.append(revisit_card(board, known, run_id, planned, published))
                continue
            key = f"{prefix}-{next_card_number}"
            next_card_number += 1
            outcomes.append(make_card(board, key, run_id, planned, published))
            write_log(log_path, published, local_ids_of_run)
        write_log(log_path, published, local_ids_of_run)
        return outcomes


def report(outcomes: list[CardOutcome]) -> None:
    ordered = sorted(outcomes, key=lambda o: (o.phase is None, o.phase or 0, key_number(o.key)))
    key_width = max(len(outcome.key) for outcome in ordered)
    status_width = max(len(word) for word in STATUS_WORDS.values())
    heading = None
    for outcome in ordered:
        here = BACKLOG if outcome.phase is None else f"Фаза {outcome.phase}"
        if here != heading:
            print(f"\n{here}")
            heading = here
        word = STATUS_WORDS[outcome.status]
        print(f"  {outcome.key:<{key_width}}  {word:<{status_width}}  {outcome.url}")


def report_task(outcome: CardOutcome) -> None:
    print(f"\n{ASSIGNMENTS_LIST}")
    print(f"  {outcome.key}  {STATUS_WORDS[outcome.status]}  {outcome.url}")


USAGE = "usage: python -m app.publish outputs/issues.json | outputs/steps.json"


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print(USAGE, file=sys.stderr)
        return EXIT_USAGE

    path = Path(arguments[0])
    if path.name not in (ISSUES_FILE, STEPS_FILE):
        # Форма публикации по содержимому JSON угадывалась бы и сломалась на первом же поле с
        # тем же именем, а имя файла-контракта её называет.
        print(
            f"{path.name}: публикуется {ISSUES_FILE} пути идеи или {STEPS_FILE} поручения",
            USAGE,
            sep="\n",
            file=sys.stderr,
        )
        return EXIT_USAGE
    try:
        path.read_text(encoding="utf-8")
    except OSError as error:
        print(f"не читается {path}: {error.strerror}", file=sys.stderr)
        return EXIT_USAGE

    try:
        if path.name == STEPS_FILE:
            report_task(publish_task(path))
        else:
            report(publish(path))
    except (InvalidIssues, InvalidSteps) as invalid:
        print(f"{path}: проблем {len(invalid.problems)}", file=sys.stderr)
        for problem in invalid.problems:
            print(f"  {problem}", file=sys.stderr)
        return EXIT_FAILED
    except (MissingRunId, MissingTask, ConfigError, TrelloError, httpx.HTTPError) as error:
        logger.error("%s", error)
        return EXIT_FAILED
    return EXIT_OK


if __name__ == "__main__":
    logging.basicConfig(format="%(levelname)s %(message)s")
    logging.getLogger("app").setLevel(logging.INFO)
    sys.exit(main())
