"""publish: issues.json → списки, карточки, чеклисты DoD и лейблы Trello. См. SPEC.md §6.

python -m app.publish outputs/issues.json

Что уже опубликовано, знает доска: в description каждой карточки стоит маркер dictum:<id>.
Рядом с issues.json пишется publish.json — кэш этого отображения для человека, не источник.
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

from app.config import ConfigError
from app.models import Area, Deferred, Issue, IssuesFile, Phase
from app.trello import Trello, TrelloCard, open_trello
from app.validate import check_issues

logger = logging.getLogger(__name__)

BACKLOG = "Backlog"
DOD = "DoD"

LabelName = Area | Literal["deferred"]
DEFERRED: LabelName = "deferred"

# Маркер стоит последней строкой, но описание могло уехать в веб-редактор Trello и вернуться
# с отступами или CRLF, а строку dictum: мог процитировать и сам текст issue: берётся последняя.
MARKER = re.compile(r"^[ \t]*dictum:(\S+)[ \t\r]*$", re.MULTILINE)

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


class InvalidIssues(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__(f"проблем {len(problems)}")
        self.problems = problems


class PublishedCard(BaseModel):
    card_id: str
    url: str


class PlannedCard(BaseModel):
    key: str
    phase: int | None
    list_id: str
    name: str
    description: str
    label_ids: list[str]
    checklist: list[str]
    depends_on: list[str]


class CardOutcome(BaseModel):
    key: str
    phase: int | None
    status: Status
    url: str


def phase_list_name(phase: Phase) -> str:
    return f"Phase {phase.n}: {phase.title}"


def card_description(issue: Issue) -> str:
    lines = [issue.description, "", f"scope_id: {issue.scope_id}"]
    if issue.depends_on:
        lines.append("Depends on: " + ", ".join(issue.depends_on))
    lines.append(f"dictum:{issue.id}")
    return "\n".join(lines)


def deferred_description(entry: Deferred) -> str:
    return f"{entry.reason}\n\ndictum:{entry.scope_id}"


def published_cards(cards: list[TrelloCard]) -> dict[str, TrelloCard]:
    found: dict[str, TrelloCard] = {}
    for card in cards:
        markers = MARKER.findall(card.desc)
        if markers:
            found[markers[-1]] = card
    return found


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
    planned = [
        PlannedCard(
            key=issue.id,
            phase=issue.phase,
            list_id=list_of_phase[issue.phase],
            name=issue.title,
            description=card_description(issue),
            label_ids=[labels[issue.area]],
            checklist=issue.dod,
            depends_on=issue.depends_on,
        )
        for issue in in_dependency_order(issues.issues)
    ]
    planned += [
        PlannedCard(
            key=entry.scope_id,
            phase=None,
            list_id=lists[BACKLOG],
            name=entry.scope_id,
            description=deferred_description(entry),
            label_ids=[labels[DEFERRED]],
            checklist=[],
            depends_on=[],
        )
        for entry in issues.deferred
    ]
    return planned


def same_card(known: TrelloCard, planned: PlannedCard) -> bool:
    return (
        known.name.strip() == planned.name.strip()
        and known.desc.strip() == planned.description.strip()
        and known.list_id == planned.list_id
        and sorted(known.label_ids) == sorted(planned.label_ids)
    )


def finish_card(
    board: Trello, known: TrelloCard, planned: PlannedCard, journal: dict[str, PublishedCard]
) -> bool:
    """Дособирает карточку, которую оборвавшийся прогон успел создать, но не успел наполнить."""
    finished = False
    if planned.checklist and not any(item.name == DOD for item in known.checklists):
        board.add_checklist(known.id, DOD, planned.checklist)
        finished = True
    attached = {item.name for item in known.attachments}
    for dependency in planned.depends_on:
        if dependency not in attached:
            board.attach_url(known.id, journal[dependency].url, dependency)
            finished = True
    return finished


def write_log(path: Path, journal: dict[str, PublishedCard], keys: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {key: journal[key].model_dump() for key in keys if key in journal}
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def publish(issues_text: str, log_path: Path) -> list[CardOutcome]:
    problems = check_issues(issues_text)
    if problems:
        raise InvalidIssues(problems)
    issues = IssuesFile.model_validate_json(issues_text)

    with closing(open_trello()) as board:
        on_board = published_cards(board.cards())
        journal = {
            key: PublishedCard(card_id=card.id, url=card.url) for key, card in on_board.items()
        }
        lists = ensure_lists(board, issues.phases)
        areas: list[LabelName] = sorted({issue.area for issue in issues.issues})
        labels = ensure_labels(board, areas + ([DEFERRED] if issues.deferred else []))

        planned_cards = plan_cards(issues, lists, labels)
        mine = [planned.key for planned in planned_cards]
        outcomes: list[CardOutcome] = []
        for planned in planned_cards:
            known = on_board.get(planned.key)
            if known:
                status: Status = "existing" if same_card(known, planned) else "differs"
                if finish_card(board, known, planned, journal):
                    status = "completed"
                outcomes.append(
                    CardOutcome(
                        key=planned.key, phase=planned.phase, status=status, url=known.url
                    )
                )
                continue
            card = board.create_card(
                planned.list_id, planned.name, planned.description, planned.label_ids
            )
            if planned.checklist:
                board.add_checklist(card.id, DOD, planned.checklist)
            for dependency in planned.depends_on:
                board.attach_url(card.id, journal[dependency].url, dependency)
            journal[planned.key] = PublishedCard(card_id=card.id, url=card.url)
            write_log(log_path, journal, mine)
            logger.info("Создана карточка %s: %s", planned.key, card.url)
            outcomes.append(
                CardOutcome(key=planned.key, phase=planned.phase, status="created", url=card.url)
            )
        write_log(log_path, journal, mine)
        return outcomes


def report(outcomes: list[CardOutcome]) -> None:
    ordered = sorted(outcomes, key=lambda o: (o.phase is None, o.phase or 0, o.key))
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
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        print(f"не читается {path}: {error.strerror}", file=sys.stderr)
        return EXIT_USAGE

    try:
        outcomes = publish(text, path.parent / "publish.json")
    except InvalidIssues as invalid:
        print(f"{path}: проблем {len(invalid.problems)}", file=sys.stderr)
        for problem in invalid.problems:
            print(f"  {problem}", file=sys.stderr)
        return EXIT_FAILED
    except (ConfigError, httpx.HTTPError) as error:
        logger.error("%s", error)
        return EXIT_FAILED
    report(outcomes)
    return EXIT_OK


if __name__ == "__main__":
    logging.basicConfig(format="%(levelname)s %(message)s")
    logging.getLogger("app").setLevel(logging.INFO)
    sys.exit(main())
