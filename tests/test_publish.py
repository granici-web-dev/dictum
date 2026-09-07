import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.config import LiveApiNotAllowed, settings
from app.models import Issue, IssuesFile
from app.publish import (
    CardOutcome,
    InvalidIssues,
    card_description,
    in_dependency_order,
    main,
    phase_list_name,
    publish,
    published_cards,
)
from app.trello import TrelloCard
from tests.helpers import BROKEN_ISSUES, REAL_ISSUES, FakeBoard, real_issues


def one(issues: IssuesFile, issue_id: str) -> Issue:
    return next(issue for issue in issues.issues if issue.id == issue_id)


def by_key(outcomes: list[CardOutcome]) -> dict[str, CardOutcome]:
    return {outcome.key: outcome for outcome in outcomes}


def put_on_board(board: FakeBoard, issue: Issue, **changes: Any) -> dict[str, Any]:
    """Кладёт на доску карточку, какой её сделал бы прошлый прогон: список и лейбл фазы на месте."""
    phase = next(item for item in real_issues().phases if item.n == issue.phase)
    fields: dict[str, Any] = {
        "name": issue.title,
        "desc": card_description(issue),
        "list_id": board.add_list(phase_list_name(phase))["id"],
        "label_ids": board.add_label(issue.area)["id"],
    }
    return board.add_card(**{**fields, **changes})


def test_publish_creates_backlog_and_a_list_per_phase(board: FakeBoard, tmp_path: Path) -> None:
    publish(REAL_ISSUES, tmp_path / "publish.json")

    assert [fields["name"] for fields in board.posted("/1/lists")] == [
        "Backlog",
        "Phase 1: Сквозной сценарий",
        "Phase 2: Обработка краевых случаев и гибкость расписания",
    ]


def test_publish_reuses_an_existing_list_with_the_same_name(
    board: FakeBoard, tmp_path: Path
) -> None:
    board.add_list("Phase 1: Сквозной сценарий")

    publish(REAL_ISSUES, tmp_path / "publish.json")

    assert [fields["name"] for fields in board.posted("/1/lists")] == [
        "Backlog",
        "Phase 2: Обработка краевых случаев и гибкость расписания",
    ]


def test_publish_creates_one_label_per_area(board: FakeBoard, tmp_path: Path) -> None:
    publish(REAL_ISSUES, tmp_path / "publish.json")

    posted = board.posted("/1/labels")
    assert [fields["name"] for fields in posted] == ["backend", "infra", "deferred"]
    assert [fields["color"] for fields in posted] == ["green", "orange", "black"]


def test_publish_writes_the_description_in_the_agreed_order(
    board: FakeBoard, tmp_path: Path
) -> None:
    issues = real_issues()

    publish(REAL_ISSUES, tmp_path / "publish.json")

    dependent = one(issues, "I-006")
    assert board.card_named(dependent.title)["desc"] == (
        f"{dependent.description}\n"
        "\n"
        f"scope_id: {dependent.scope_id}\n"
        "Depends on: I-004, I-010\n"
        "dictum:I-006"
    )


def test_publish_leaves_out_the_depends_on_line_when_there_is_nothing_to_depend_on(
    board: FakeBoard, tmp_path: Path
) -> None:
    issues = real_issues()

    publish(REAL_ISSUES, tmp_path / "publish.json")

    independent = one(issues, "I-001")
    assert independent.depends_on == []
    assert board.card_named(independent.title)["desc"] == (
        f"{independent.description}\n\nscope_id: {independent.scope_id}\ndictum:I-001"
    )


def test_publish_adds_a_dod_checklist_with_every_item(board: FakeBoard, tmp_path: Path) -> None:
    issues = real_issues()

    publish(REAL_ISSUES, tmp_path / "publish.json")

    first = issues.issues[0]
    card = board.card_named(first.title)
    assert [item["name"] for item in card["checklists"]] == ["DoD"]
    checklist_id = card["checklists"][0]["id"]
    items = board.posted(f"/1/checklists/{checklist_id}/checkItems")
    assert [fields["name"] for fields in items] == first.dod


def test_publish_attaches_the_card_of_every_dependency(board: FakeBoard, tmp_path: Path) -> None:
    issues = real_issues()

    publish(REAL_ISSUES, tmp_path / "publish.json")

    dependent = board.card_named(one(issues, "I-006").title)
    dependency = board.card_named(one(issues, "I-010").title)
    assert {"url": dependency["url"], "name": "I-010"} in board.posted(
        f"/1/cards/{dependent['id']}/attachments"
    )


def test_publish_lays_cards_out_in_the_order_of_the_file(
    board: FakeBoard, tmp_path: Path
) -> None:
    issues = real_issues()

    publish(REAL_ISSUES, tmp_path / "publish.json")

    created = board.posted("/1/cards")
    ordered = [fields["name"] for fields in created]
    dependent, dependency = one(issues, "I-006"), one(issues, "I-010")
    assert ordered.index(dependency.title) < ordered.index(dependent.title)
    position = {fields["name"]: int(fields["pos"]) for fields in created}
    assert position[dependent.title] < position[dependency.title]
    assert [position[issue.title] for issue in issues.issues] == list(
        range(1, len(issues.issues) + 1)
    )


def test_publish_orders_every_issue_after_all_of_its_dependencies() -> None:
    issues = real_issues()
    ordered = [issue.id for issue in in_dependency_order(issues.issues)]

    assert sorted(ordered) == sorted(issue.id for issue in issues.issues)
    for issue in issues.issues:
        for dependency in issue.depends_on:
            assert ordered.index(dependency) < ordered.index(issue.id)


def test_publish_skips_an_issue_whose_marker_is_already_on_the_board(
    board: FakeBoard, tmp_path: Path
) -> None:
    issues = real_issues()
    first = issues.issues[0]
    known = put_on_board(board, first)
    known["checklists"].append({"id": "checklist-0", "name": "DoD"})

    outcomes = publish(REAL_ISSUES, tmp_path / "publish.json")

    assert first.title not in [fields["name"] for fields in board.posted("/1/cards")]
    assert by_key(outcomes)[first.id] == CardOutcome(
        key=first.id, phase=first.phase, status="existing", url=known["url"]
    )


def test_publish_skips_an_issue_whose_card_was_archived(board: FakeBoard, tmp_path: Path) -> None:
    issues = real_issues()
    first = issues.issues[0]
    archived = put_on_board(board, first, archived=True)
    archived["checklists"].append({"id": "checklist-0", "name": "DoD"})

    outcomes = publish(REAL_ISSUES, tmp_path / "publish.json")

    assert first.title not in [fields["name"] for fields in board.posted("/1/cards")]
    assert by_key(outcomes)[first.id].status == "existing"


def test_publish_marks_a_changed_issue_as_differing_and_leaves_the_card_alone(
    board: FakeBoard, tmp_path: Path
) -> None:
    issues = real_issues()
    first = issues.issues[0]
    known = put_on_board(board, first, desc="Описания больше нет в issues.json\n\ndictum:I-001")
    known["checklists"].append({"id": "checklist-0", "name": "DoD"})

    outcomes = publish(REAL_ISSUES, tmp_path / "publish.json")

    assert first.title not in [fields["name"] for fields in board.posted("/1/cards")]
    assert by_key(outcomes)[first.id].status == "differs"


def test_publish_notices_an_issue_that_moved_to_another_list(
    board: FakeBoard, tmp_path: Path
) -> None:
    issues = real_issues()
    first = issues.issues[0]
    known = put_on_board(board, first, list_id="список-из-прошлой-жизни")
    known["checklists"].append({"id": "checklist-0", "name": "DoD"})

    outcomes = publish(REAL_ISSUES, tmp_path / "publish.json")

    assert by_key(outcomes)[first.id].status == "differs"


def test_publish_finishes_a_card_left_without_its_checklist(
    board: FakeBoard, tmp_path: Path
) -> None:
    issues = real_issues()
    first = issues.issues[0]
    half_made = put_on_board(board, first)

    outcomes = publish(REAL_ISSUES, tmp_path / "publish.json")

    assert board.posted(f"/1/cards/{half_made['id']}/checklists") == [{"name": "DoD"}]
    assert by_key(outcomes)[first.id].status == "completed"


def test_publish_finishes_a_card_left_without_its_dependency_links(
    board: FakeBoard, tmp_path: Path
) -> None:
    issues = real_issues()
    dependent = one(issues, "I-006")
    half_made = put_on_board(board, dependent)
    half_made["checklists"].append({"id": "checklist-0", "name": "DoD"})
    half_made["attachments"].append({"id": "attachment-0", "name": "I-004"})

    outcomes = publish(REAL_ISSUES, tmp_path / "publish.json")

    attached = board.posted(f"/1/cards/{half_made['id']}/attachments")
    assert [fields["name"] for fields in attached] == ["I-010"]
    assert by_key(outcomes)[dependent.id].status == "completed"


def test_publish_puts_deferred_scope_in_backlog_without_a_checklist(
    board: FakeBoard, tmp_path: Path
) -> None:
    issues = real_issues()
    deferred = issues.deferred[0]

    outcomes = publish(REAL_ISSUES, tmp_path / "publish.json")

    backlog = next(item for item in board.lists if item["name"] == "Backlog")
    label = next(item for item in board.labels if item["name"] == "deferred")
    card = next(
        fields for fields in board.posted("/1/cards") if fields["name"] == deferred.scope_id
    )
    assert card["idList"] == backlog["id"]
    assert card["idLabels"] == label["id"]
    assert deferred.reason in card["desc"]
    assert not board.card_named(deferred.scope_id)["checklists"]
    assert by_key(outcomes)[deferred.scope_id].phase is None


def test_publish_records_the_cards_it_made_before_a_later_one_failed(
    board: FakeBoard, tmp_path: Path
) -> None:
    log_path = tmp_path / "publish.json"
    board.fail_after_cards = 3

    with pytest.raises(httpx.HTTPStatusError):
        publish(REAL_ISSUES, log_path)

    journal = json.loads(log_path.read_text(encoding="utf-8"))
    assert len(journal) == 3
    assert all(entry["url"].startswith("https://trello.com/c/") for entry in journal.values())


def test_publish_logs_every_issue_and_deferred_scope(board: FakeBoard, tmp_path: Path) -> None:
    issues = real_issues()
    log_path = tmp_path / "publish.json"

    publish(REAL_ISSUES, log_path)

    journal = json.loads(log_path.read_text(encoding="utf-8"))
    assert set(journal) == {issue.id for issue in issues.issues} | {
        entry.scope_id for entry in issues.deferred
    }


def test_publish_keeps_other_runs_out_of_the_log(board: FakeBoard, tmp_path: Path) -> None:
    log_path = tmp_path / "publish.json"
    board.add_card("Карточка чужого прогона", "текст\n\ndictum:I-777")

    publish(REAL_ISSUES, log_path)

    assert "I-777" not in json.loads(log_path.read_text(encoding="utf-8"))


def test_publish_writes_the_log_even_when_it_creates_nothing(
    board: FakeBoard, tmp_path: Path
) -> None:
    log_path = tmp_path / "publish.json"
    publish(REAL_ISSUES, log_path)
    log_path.unlink()
    created_before = len(board.posted("/1/cards"))

    publish(REAL_ISSUES, log_path)

    assert len(board.posted("/1/cards")) == created_before
    assert json.loads(log_path.read_text(encoding="utf-8"))


def test_publish_sends_nothing_without_allow_live_api(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "allow_live_api", False)

    with pytest.raises(LiveApiNotAllowed):
        publish(REAL_ISSUES, tmp_path / "publish.json")

    assert not respx_mock.calls


def test_publish_refuses_an_invalid_issues_file_before_touching_the_board(
    respx_mock: respx.MockRouter, tmp_path: Path
) -> None:
    with pytest.raises(InvalidIssues) as refused:
        publish(BROKEN_ISSUES, tmp_path / "publish.json")

    assert any("title" in problem for problem in refused.value.problems)
    assert not respx_mock.calls


def test_marker_survives_indentation_carriage_returns_and_quotation() -> None:
    def card(desc: str) -> TrelloCard:
        return TrelloCard(id="card-1", name="имя", desc=desc, url="u", idList="list-1")

    assert published_cards([card("текст\r\n\r\ndictum:I-001\r\n")])["I-001"].id == "card-1"
    assert published_cards([card("текст\n  dictum:I-001  ")])["I-001"].id == "card-1"
    quoted = "Как в dictum:I-999, только наоборот\ndictum:I-999\n\nтекст\n\ndictum:I-001"
    assert published_cards([card(quoted)])["I-001"].id == "card-1"
    assert "I-999" not in published_cards([card(quoted)])


def test_main_reports_the_problems_of_an_invalid_issues_file(
    respx_mock: respx.MockRouter, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "issues.json"
    path.write_text(BROKEN_ISSUES, encoding="utf-8")

    assert main([str(path)]) == 1
    assert "title" in capsys.readouterr().err
    assert not respx_mock.calls


def test_main_writes_the_log_next_to_the_issues_file(
    board: FakeBoard, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "issues.json"
    path.write_text(REAL_ISSUES, encoding="utf-8")

    assert main([str(path)]) == 0

    assert (tmp_path / "publish.json").exists()
    assert "Фаза 1" in capsys.readouterr().out


def test_main_reports_a_board_that_fell_over(board: FakeBoard, tmp_path: Path) -> None:
    path = tmp_path / "issues.json"
    path.write_text(REAL_ISSUES, encoding="utf-8")
    board.fail_after_cards = 1

    assert main([str(path)]) == 1


def test_main_without_a_readable_file_is_a_usage_error(tmp_path: Path) -> None:
    assert main([]) == 64
    assert main([str(tmp_path / "нет-такого.json")]) == 64
