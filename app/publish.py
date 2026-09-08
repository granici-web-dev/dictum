"""publish: issues.json → списки, карточки, чеклисты DoD и лейблы Trello. См. SPEC.md §6.

python -m app.publish outputs/issues.json

Что уже опубликовано, знает доска: в description каждой карточки стоит маркер
dictum:<KEY-N> run:<run_id> local:<I-00N|S<N>>. Рядом с issues.json пишется publish.json —
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
from app.trello import Trello, TrelloCard, TrelloChecklist, TrelloError, open_trello
from app.validate import check_issues

logger = logging.getLogger(__name__)

BACKLOG = "Backlog"
DOD = "DoD"

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
    def __init__(self, path: Path) -> None:
        super().__init__(
            f"в {path} нет run_id. Его проставляет decompose из transcript.md (SPEC §3.1); "
            "publish своего не выдаёт, иначе у прогона было бы два разных номера."
        )


class InvalidIssues(ValueError):
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
    body: str
    scope_id: str
    label_ids: list[str]
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
    lines = [planned.body, "", f"scope_id: {planned.scope_id}"]
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
            by_name[name] = board.create_list(name).id
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
            body=issue.description,
            scope_id=issue.scope_id,
            label_ids=[labels[issue.area]],
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
            body=entry.reason,
            scope_id=entry.scope_id,
            label_ids=[labels[DEFERRED]],
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
    board: Trello, card_id: str, wanted: list[str], existing: TrelloChecklist | None
) -> bool:
    """Доводит чеклист DoD до полного набора пунктов, чем бы ни кончился прошлый прогон."""
    if not wanted:
        return False
    checklist = existing or board.create_checklist(card_id, DOD)
    present = {item.name for item in checklist.check_items}
    missing = [item for item in wanted if item not in present]
    for item in missing:
        board.add_check_item(checklist.id, item)
    return bool(missing)


def finish_card(
    board: Trello, known: TrelloCard, planned: PlannedCard, journal: dict[str, PublishedCard]
) -> bool:
    """Дособирает карточку, которую оборвавшийся прогон успел создать, но не успел наполнить."""
    checklist = next((item for item in known.checklists if item.name == DOD), None)
    finished = fill_checklist(board, known.id, planned.checklist, checklist)
    attached = {item.name for item in known.attachments}
    for dependency in planned.depends_on:
        published = journal[dependency]
        if published.key not in attached:
            board.attach_url(known.id, published.url, published.key)
            finished = True
    return finished


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
    # Дособранная карточка, которая вдобавок разошлась с файлом, остаётся differs:
    # человеку важнее знать про расхождение, чем про дописанный чеклист.
    if finish_card(board, known.card, planned, published) and status == "existing":
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
    fill_checklist(board, card.id, planned.checklist, None)
    for dependency in planned.depends_on:
        linked = published[dependency]
        board.attach_url(card.id, linked.url, linked.key)
    published[planned.local_id] = PublishedCard(key=key, card_id=card.id, url=card.url)
    logger.info("Создана карточка %s (%s): %s", key, planned.local_id, card.url)
    return CardOutcome(
        local_id=planned.local_id, key=key, phase=planned.phase, status="created", url=card.url
    )


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
    log_path = issues_path.parent / "publish.json"

    with closing(open_trello()) as board:
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


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("usage: python -m app.publish outputs/issues.json", file=sys.stderr)
        return EXIT_USAGE

    path = Path(arguments[0])
    try:
        path.read_text(encoding="utf-8")
    except OSError as error:
        print(f"не читается {path}: {error.strerror}", file=sys.stderr)
        return EXIT_USAGE

    try:
        outcomes = publish(path)
    except InvalidIssues as invalid:
        print(f"{path}: проблем {len(invalid.problems)}", file=sys.stderr)
        for problem in invalid.problems:
            print(f"  {problem}", file=sys.stderr)
        return EXIT_FAILED
    except (MissingRunId, ConfigError, TrelloError, httpx.HTTPError) as error:
        logger.error("%s", error)
        return EXIT_FAILED
    report(outcomes)
    return EXIT_OK


if __name__ == "__main__":
    logging.basicConfig(format="%(levelname)s %(message)s")
    logging.getLogger("app").setLevel(logging.INFO)
    sys.exit(main())
