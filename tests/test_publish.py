import json
from pathlib import Path
from typing import Any

import pytest
import respx

from app.config import InvalidProjectKey, LiveApiNotAllowed, settings
from app.models import Issue, IssuesFile
from app.publish import (
    CardOutcome,
    PublishedCard,
    InvalidIssues,
    MissingRunId,
    PlannedCard,
    marked_cards,
    card_description,
    card_name,
    in_dependency_order,
    main,
    phase_list_name,
    publish,
    report,
)
from app.trello import TrelloCard, TrelloError
from tests.helpers import (
    BROKEN_ISSUES,
    DEPENDENCY_BELOW,
    FakeBoard,
    issues_file,
    real_issues,
)


def one(issues: IssuesFile, issue_id: str) -> Issue:
    return next(issue for issue in issues.issues if issue.id == issue_id)


def by_local(outcomes: list[CardOutcome]) -> dict[str, CardOutcome]:
    return {outcome.local_id: outcome for outcome in outcomes}


def put_on_board(
    board: FakeBoard,
    issue: Issue,
    key: str,
    run_id: str,
    keys: dict[str, str] | None = None,
    dod: list[str] | None = None,
    **changes: Any,
) -> dict[str, Any]:
    """Кладёт карточку, какой её оставил бы прошлый прогон: имя, маркер, DoD и ссылки на месте."""
    issues = real_issues()
    linked = {
        local: PublishedCard(key=key_of, card_id="card-known", url=f"https://trello.com/c/{key_of}")
        for local, key_of in (keys or {}).items()
    }
    phase = next(item for item in issues.phases if item.n == issue.phase)
    planned = PlannedCard(
        local_id=issue.id,
        phase=issue.phase,
        list_id=board.list_named(phase_list_name(phase))["id"],
        title=issue.title,
        body=f"{issue.description}\n\nscope_id: {issue.scope_id}",
        label_ids=[board.label_named(issue.area)["id"]],
        checklist_name="DoD",
        checklist=issue.dod,
        depends_on=issue.depends_on,
        position=1,
    )
    fields: dict[str, Any] = {
        "name": card_name(key, planned.title),
        "desc": card_description(key, run_id, planned, linked),
        "list_id": planned.list_id,
        "label_ids": planned.label_ids[0],
    }
    card = board.add_card(**{**fields, **changes})
    board.put_checklist(card, "DoD", issue.dod if dod is None else dod)
    board.put_attachments(card, [linked[local].key for local in issue.depends_on])
    return card


def test_publish_creates_backlog_and_a_list_per_phase(board: FakeBoard, tmp_path: Path) -> None:
    publish(issues_file(tmp_path))

    assert board.list_names() == ["Backlog"] + [
        f"Phase {phase.n}: {phase.title}" for phase in real_issues().phases
    ]


def test_publish_reuses_an_existing_list_with_the_same_name(
    board: FakeBoard, tmp_path: Path
) -> None:
    board.add_list("Phase 1: Сквозной сценарий")

    publish(issues_file(tmp_path))

    second = real_issues().phases[1]
    assert [fields["name"] for fields in board.posted("/1/lists")] == [
        "Backlog",
        f"Phase {second.n}: {second.title}",
    ]


def test_publish_creates_one_label_per_area(board: FakeBoard, tmp_path: Path) -> None:
    publish(issues_file(tmp_path))

    posted = board.posted("/1/labels")
    assert [fields["name"] for fields in posted] == ["backend", "infra", "deferred"]
    assert [fields["color"] for fields in posted] == ["green", "orange", "black"]


def test_publish_names_every_card_with_its_key(board: FakeBoard, tmp_path: Path) -> None:
    issues = real_issues()

    outcomes = publish(issues_file(tmp_path))

    first = issues.issues[0]
    assert by_local(outcomes)[first.id].key == "DCT-1"
    assert board.card_named(f"[DCT-1] {first.title}")


def test_publish_continues_the_numbering_the_board_already_carries(
    board: FakeBoard, tmp_path: Path
) -> None:
    board.add_card("[DCT-40] Чужая карточка", "текст\n\ndictum:DCT-40 run:другой local:I-001")

    outcomes = publish(issues_file(tmp_path))

    assert [outcome.key for outcome in outcomes][:2] == ["DCT-41", "DCT-42"]


def test_publish_numbers_deferred_scopes_from_the_same_counter(
    board: FakeBoard, tmp_path: Path
) -> None:
    issues = real_issues()

    outcomes = publish(issues_file(tmp_path))

    issued = [outcome.key for outcome in outcomes]
    assert issued == [f"DCT-{number}" for number in range(1, len(issued) + 1)]
    deferred = issues.deferred[0]
    assert by_local(outcomes)[deferred.scope_id].key == f"DCT-{len(issues.issues) + 1}"
    assert board.card_named(card_name(f"DCT-{len(issues.issues) + 1}", deferred.title))


def test_publish_writes_the_description_with_global_dependencies_and_the_marker(
    board: FakeBoard, tmp_path: Path
) -> None:
    issues = real_issues()

    outcomes = publish(issues_file(tmp_path))

    keys = {local: outcome.key for local, outcome in by_local(outcomes).items()}
    dependent = one(issues, "I-008")
    card = board.card_named(card_name(keys["I-008"], dependent.title))
    expected_dependencies = ", ".join(keys[local] for local in dependent.depends_on)
    assert card["desc"].startswith(dependent.description)
    assert f"scope_id: {dependent.scope_id}" in card["desc"]
    assert f"Depends on: {expected_dependencies}" in card["desc"]
    assert card["desc"].endswith(f"dictum:{keys['I-008']} run:{run_id_of(tmp_path)} local:I-008")


def run_id_of(directory: Path) -> str:
    stamped = json.loads((directory / "issues.json").read_text(encoding="utf-8"))
    run_id: str = stamped["run_id"]
    return run_id


def test_publish_leaves_out_the_depends_on_line_when_there_is_nothing_to_depend_on(
    board: FakeBoard, tmp_path: Path
) -> None:
    issues = real_issues()

    outcomes = publish(issues_file(tmp_path))

    independent = one(issues, "I-001")
    assert independent.depends_on == []
    card = board.card_named(card_name(by_local(outcomes)["I-001"].key, independent.title))
    assert "Depends on:" not in card["desc"]


def test_publish_attaches_every_dependency_under_its_global_key(
    board: FakeBoard, tmp_path: Path
) -> None:
    issues = real_issues()

    outcomes = publish(issues_file(tmp_path))

    keys = {local: outcome.key for local, outcome in by_local(outcomes).items()}
    dependent = one(issues, "I-008")
    card = board.card_named(card_name(keys["I-008"], dependent.title))
    attached = board.posted(f"/1/cards/{card['id']}/attachments")
    assert [fields["name"] for fields in attached] == [
        keys[local] for local in dependent.depends_on
    ]


def test_publish_refuses_a_file_without_a_run_id(board: FakeBoard, tmp_path: Path) -> None:
    path = issues_file(tmp_path, run_id=None)

    with pytest.raises(MissingRunId, match="нет run_id"):
        publish(path)

    assert board.posted("/1/cards") == []


def test_publish_never_writes_to_the_issues_file(board: FakeBoard, tmp_path: Path) -> None:
    path = issues_file(tmp_path, run_id="прогон-семнадцать")
    before = path.read_text(encoding="utf-8")

    publish(path)
    publish(path)

    assert path.read_text(encoding="utf-8") == before
    assert len(board.posted("/1/cards")) == len(real_issues().issues) + len(real_issues().deferred)


def test_publish_finds_its_cards_on_the_board_without_a_journal(
    board: FakeBoard, tmp_path: Path
) -> None:
    issues = real_issues()
    first = issues.issues[0]
    known = put_on_board(board, first, "DCT-7", "прогон-семь")
    path = issues_file(tmp_path, run_id="прогон-семь")
    assert not (tmp_path / "publish.json").exists()

    outcomes = publish(path)

    assert card_name("DCT-7", first.title) not in [
        fields["name"] for fields in board.posted("/1/cards")
    ]
    assert by_local(outcomes)[first.id] == CardOutcome(
        local_id=first.id, key="DCT-7", phase=first.phase, status="existing", url=known["url"]
    )


def test_two_runs_of_the_same_local_ids_get_two_cards(board: FakeBoard, tmp_path: Path) -> None:
    issues = real_issues()
    first = issues.issues[0]
    earlier = tmp_path / "earlier"
    later = tmp_path / "later"
    earlier.mkdir()
    later.mkdir()

    first_run = publish(issues_file(earlier, run_id="прогон-первый"))
    second_run = publish(issues_file(later, run_id="прогон-второй"))

    assert run_id_of(earlier) != run_id_of(later)
    assert by_local(first_run)[first.id].key != by_local(second_run)[first.id].key
    made = [fields["name"] for fields in board.posted("/1/cards")]
    assert sum(1 for name in made if name.endswith(first.title)) == 2


def test_publish_adds_a_dod_checklist_with_every_item(board: FakeBoard, tmp_path: Path) -> None:
    issues = real_issues()

    outcomes = publish(issues_file(tmp_path))

    first = issues.issues[0]
    card = board.card_named(card_name(by_local(outcomes)[first.id].key, first.title))
    assert [item["name"] for item in card["checklists"]] == ["DoD"]
    checklist_id = card["checklists"][0]["id"]
    items = board.posted(f"/1/checklists/{checklist_id}/checkItems")
    assert [fields["name"] for fields in items] == first.dod


def test_publish_adds_the_dod_items_a_broken_run_never_wrote(
    board: FakeBoard, tmp_path: Path
) -> None:
    issues = real_issues()
    first = issues.issues[0]
    half_made = put_on_board(board, first, "DCT-3", "прогон-три", dod=first.dod[1:])
    checklist_id = half_made["checklists"][0]["id"]

    outcomes = publish(issues_file(tmp_path, run_id="прогон-три"))

    added = board.posted(f"/1/checklists/{checklist_id}/checkItems")
    assert [fields["name"] for fields in added] == first.dod[:1]
    assert by_local(outcomes)[first.id].status == "completed"


def test_publish_finishes_a_card_left_without_its_dependency_links(
    board: FakeBoard, tmp_path: Path
) -> None:
    issues = real_issues()
    dependent = one(issues, "I-002")
    keys = {"I-001": "DCT-1"}
    half_made = put_on_board(board, dependent, "DCT-2", "прогон-два", keys=keys)
    half_made["attachments"].clear()
    put_on_board(board, one(issues, "I-001"), "DCT-1", "прогон-два")

    outcomes = publish(issues_file(tmp_path, run_id="прогон-два"))

    attached = board.posted(f"/1/cards/{half_made['id']}/attachments")
    assert [fields["name"] for fields in attached] == ["DCT-1"]
    assert by_local(outcomes)[dependent.id].status == "completed"


def test_publish_marks_a_changed_issue_as_differing_and_leaves_the_card_alone(
    board: FakeBoard, tmp_path: Path
) -> None:
    issues = real_issues()
    first = issues.issues[0]
    put_on_board(
        board,
        first,
        "DCT-9",
        "прогон-девять",
        desc="Описания больше нет\n\ndictum:DCT-9 run:прогон-девять local:I-001",
    )

    outcomes = publish(issues_file(tmp_path, run_id="прогон-девять"))

    assert by_local(outcomes)[first.id].status == "differs"
    assert card_name("DCT-9", first.title) not in [
        fields["name"] for fields in board.posted("/1/cards")
    ]


def test_publish_notices_a_card_that_moved_to_another_list(
    board: FakeBoard, tmp_path: Path
) -> None:
    issues = real_issues()
    first = issues.issues[0]
    put_on_board(board, first, "DCT-9", "прогон-девять", list_id="список-из-прошлой-жизни")

    outcomes = publish(issues_file(tmp_path, run_id="прогон-девять"))

    assert by_local(outcomes)[first.id].status == "differs"


def test_a_card_that_both_differs_and_lacks_its_checklist_reports_the_difference(
    board: FakeBoard, tmp_path: Path
) -> None:
    issues = real_issues()
    first = issues.issues[0]
    put_on_board(
        board,
        first,
        "DCT-9",
        "прогон-девять",
        dod=[],
        name="[DCT-9] Кто-то переписал заголовок",
    )

    outcomes = publish(issues_file(tmp_path, run_id="прогон-девять"))

    assert by_local(outcomes)[first.id].status == "differs"


def test_publish_notices_a_card_that_was_renamed(board: FakeBoard, tmp_path: Path) -> None:
    issues = real_issues()
    first = issues.issues[0]
    put_on_board(board, first, "DCT-9", "прогон-девять", name="[DCT-9] Кто-то переписал заголовок")

    outcomes = publish(issues_file(tmp_path, run_id="прогон-девять"))

    assert by_local(outcomes)[first.id].status == "differs"


def test_publish_notices_a_card_that_lost_its_label(board: FakeBoard, tmp_path: Path) -> None:
    issues = real_issues()
    first = issues.issues[0]
    put_on_board(board, first, "DCT-9", "прогон-девять", label_ids="")

    outcomes = publish(issues_file(tmp_path, run_id="прогон-девять"))

    assert by_local(outcomes)[first.id].status == "differs"


def test_publish_writes_the_log_even_when_it_creates_nothing(
    board: FakeBoard, tmp_path: Path
) -> None:
    path = issues_file(tmp_path)
    publish(path)
    (tmp_path / "publish.json").unlink()
    created_before = len(board.posted("/1/cards"))

    publish(path)

    assert len(board.posted("/1/cards")) == created_before
    assert json.loads((tmp_path / "publish.json").read_text(encoding="utf-8"))


def test_publish_ignores_keys_of_other_projects_when_numbering(
    board: FakeBoard, tmp_path: Path
) -> None:
    board.add_card("[OTHER-99] Чужой проект", "т\n\ndictum:OTHER-99 run:чужой local:I-001")
    board.add_card("[DCT-5] Наш прошлый", "т\n\ndictum:DCT-5 run:прошлый local:I-002")

    outcomes = publish(issues_file(tmp_path))

    assert outcomes[0].key == "DCT-6"


def test_publish_starts_over_when_the_board_was_emptied(board: FakeBoard, tmp_path: Path) -> None:
    path = issues_file(tmp_path, run_id="прогон-которого-нет-на-доске")

    outcomes = publish(path)

    assert [outcome.status for outcome in outcomes] == ["created"] * len(outcomes)
    assert outcomes[0].key == "DCT-1"
    assert run_id_of(tmp_path) == "прогон-которого-нет-на-доске"


def test_publish_skips_an_issue_whose_card_was_archived(board: FakeBoard, tmp_path: Path) -> None:
    issues = real_issues()
    first = issues.issues[0]
    put_on_board(board, first, "DCT-5", "прогон-пять", archived=True)

    outcomes = publish(issues_file(tmp_path, run_id="прогон-пять"))

    assert by_local(outcomes)[first.id].status == "existing"


def test_publish_lays_cards_out_in_the_order_of_the_file(board: FakeBoard, tmp_path: Path) -> None:
    issues = IssuesFile.model_validate_json(DEPENDENCY_BELOW)
    dependent, dependency = one(issues, "I-006"), one(issues, "I-010")

    outcomes = publish(issues_file(tmp_path, DEPENDENCY_BELOW))

    keys = {local: outcome.key for local, outcome in by_local(outcomes).items()}
    made = [fields["name"] for fields in board.posted("/1/cards")]
    assert made.index(card_name(keys["I-010"], dependency.title)) < made.index(
        card_name(keys["I-006"], dependent.title)
    )
    first_phase = [
        card_name(keys[issue.id], issue.title) for issue in issues.issues if issue.phase == 1
    ]
    assert board.card_names("Phase 1: Сквозной сценарий") == first_phase


def test_publish_orders_every_issue_after_all_of_its_dependencies() -> None:
    issues = IssuesFile.model_validate_json(DEPENDENCY_BELOW)
    ordered = [issue.id for issue in in_dependency_order(issues.issues)]

    assert sorted(ordered) == sorted(issue.id for issue in issues.issues)
    for issue in issues.issues:
        for dependency in issue.depends_on:
            assert ordered.index(dependency) < ordered.index(issue.id)


def test_publish_puts_deferred_scope_in_backlog_without_a_checklist(
    board: FakeBoard, tmp_path: Path
) -> None:
    issues = real_issues()
    deferred = issues.deferred[0]

    outcomes = publish(issues_file(tmp_path))

    key = by_local(outcomes)[deferred.scope_id].key
    backlog = next(item for item in board.lists if item["name"] == "Backlog")
    label = next(item for item in board.labels if item["name"] == "deferred")
    card = board.card_named(card_name(key, deferred.title))
    assert card["idList"] == backlog["id"]
    assert card["idLabels"] == [label["id"]]
    assert deferred.reason in card["desc"]
    assert not card["checklists"]
    in_backlog = board.card_names("Backlog")
    assert in_backlog == [
        card_name(by_local(outcomes)[entry.scope_id].key, entry.title)
        for entry in issues.deferred
    ]
    positions = [fields["pos"] for fields in board.posted("/1/cards")][-len(issues.deferred) :]
    assert positions == sorted(positions, key=int)
    assert len(set(positions)) == len(issues.deferred)


def test_publish_logs_every_issue_and_deferred_scope(board: FakeBoard, tmp_path: Path) -> None:
    issues = real_issues()

    publish(issues_file(tmp_path))

    journal = json.loads((tmp_path / "publish.json").read_text(encoding="utf-8"))
    assert set(journal) == {issue.id for issue in issues.issues} | {
        entry.scope_id for entry in issues.deferred
    }
    assert journal["I-001"]["key"] == "DCT-1"


def test_publish_keeps_other_runs_out_of_the_log(board: FakeBoard, tmp_path: Path) -> None:
    board.add_card("[DCT-77] Чужая", "текст\n\ndictum:DCT-77 run:чужой local:I-001")

    publish(issues_file(tmp_path))

    journal = json.loads((tmp_path / "publish.json").read_text(encoding="utf-8"))
    assert all(entry["key"] != "DCT-77" for entry in journal.values())


def test_the_log_leaves_out_a_card_whose_issue_is_gone_from_the_file(
    board: FakeBoard, tmp_path: Path
) -> None:
    board.add_card(
        "[DCT-40] Issue, которого больше нет в файле",
        "т\n\ndictum:DCT-40 run:прогон-сорок local:I-404",
    )

    publish(issues_file(tmp_path, run_id="прогон-сорок"))

    journal = json.loads((tmp_path / "publish.json").read_text(encoding="utf-8"))
    assert "I-404" not in journal


def test_publish_records_the_cards_it_made_before_a_later_one_failed(
    board: FakeBoard, tmp_path: Path
) -> None:
    board.fail_after_cards = 3

    with pytest.raises(TrelloError):
        publish(issues_file(tmp_path))

    journal = json.loads((tmp_path / "publish.json").read_text(encoding="utf-8"))
    assert len(journal) == 3


def test_publish_refuses_a_project_key_that_is_not_a_key(
    board: FakeBoard, monkeypatch: pytest.MonkeyPatch, respx_mock: respx.MockRouter, tmp_path: Path
) -> None:
    for bad in ("", "dct", "D", "DCT-1", "СЛОВО"):
        monkeypatch.setattr(settings, "project_key", bad)
        with pytest.raises(InvalidProjectKey):
            publish(issues_file(tmp_path))
    assert not respx_mock.calls


def test_publish_sends_nothing_without_allow_live_api(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "allow_live_api", False)
    monkeypatch.setattr(settings, "project_key", "DCT")

    with pytest.raises(LiveApiNotAllowed):
        publish(issues_file(tmp_path))

    assert not respx_mock.calls


def test_publish_refuses_an_invalid_issues_file_before_touching_the_board(
    respx_mock: respx.MockRouter, tmp_path: Path
) -> None:
    with pytest.raises(InvalidIssues) as refused:
        publish(issues_file(tmp_path, BROKEN_ISSUES))

    assert any("title" in problem for problem in refused.value.problems)
    assert not respx_mock.calls


def test_a_marker_without_the_run_and_the_local_is_not_a_marker() -> None:
    old_format = TrelloCard(
        id="card-1", name="имя", desc="текст\n\ndictum:I-001", url="u", idList="list-1"
    )

    assert marked_cards([old_format]) == []


def test_the_report_orders_keys_by_number_not_by_text(capsys: pytest.CaptureFixture[str]) -> None:
    made = [
        CardOutcome(local_id=f"I-{n:03}", key=f"DCT-{n}", phase=1, status="created", url="u")
        for n in range(1, 13)
    ]

    report(made)

    printed = [line.split()[0] for line in capsys.readouterr().out.splitlines() if "DCT-" in line]
    assert printed == [f"DCT-{n}" for n in range(1, 13)]


def test_marker_survives_indentation_carriage_returns_and_quotation() -> None:
    def card(desc: str) -> TrelloCard:
        return TrelloCard(id="card-1", name="имя", desc=desc, url="u", idList="list-1")

    crlf = "текст\r\n\r\ndictum:DCT-1 run:abc local:I-001\r\n"
    assert marked_cards([card(crlf)])[0].key == "DCT-1"
    indented = "текст\n  dictum:DCT-1 run:abc local:I-001  "
    assert marked_cards([card(indented)])[0].local_id == "I-001"
    quoted = (
        "как в dictum:DCT-9 run:x local:I-009, только наоборот\n"
        "dictum:DCT-9 run:x local:I-009\n\nтекст\n\ndictum:DCT-1 run:abc local:I-001"
    )
    assert marked_cards([card(quoted)])[0].key == "DCT-1"


def test_main_reports_the_problems_of_an_invalid_issues_file(
    respx_mock: respx.MockRouter, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = issues_file(tmp_path, BROKEN_ISSUES)

    assert main([str(path)]) == 1
    assert "title" in capsys.readouterr().err
    assert not respx_mock.calls


def test_main_writes_the_log_next_to_the_issues_file(
    board: FakeBoard, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = issues_file(tmp_path)

    assert main([str(path)]) == 0

    assert (tmp_path / "publish.json").exists()
    assert "Фаза 1" in capsys.readouterr().out


def test_main_reports_a_board_that_fell_over(board: FakeBoard, tmp_path: Path) -> None:
    board.fail_after_cards = 1

    assert main([str(issues_file(tmp_path))]) == 1


def test_main_without_a_readable_file_is_a_usage_error(tmp_path: Path) -> None:
    assert main([]) == 64
    assert main([str(tmp_path / "нет-такого.json")]) == 64
