import json
from pathlib import Path

import pytest

from app.pipeline import (
    BRIEF,
    CANDIDATES,
    IDEA,
    ISSUES_JSON,
    PRD,
    RESEARCH,
    TRANSCRIPT,
    stage_named,
)
from app.run import Run, missing_before, walk
from tests.helpers import FakeBoard, InstallResponses, ok, real_issues
from tests.test_cli import (
    BRIEF_BLOCK,
    CANDIDATES_BLOCK,
    IDEA_BLOCK,
    ISSUES_BLOCKS,
    PRD_BLOCK,
    TEXT,
)

RUN_ID = "прогон-обхода"


@pytest.fixture(autouse=True)
def nothing_around(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Обход обязан ходить только по своему корню.

    Без этого тест, читающий мимо корня, незаметно возьмёт артефакты самого репозитория:
    inputs/transcript.md и outputs/ там лежат от прошлых прогонов.
    """
    monkeypatch.chdir(tmp_path)


def a_run(root: Path, text: str = TEXT) -> Run:
    return Run(root=root, run_id=RUN_ID, lang="ru", text=text)


def test_a_full_walk_writes_every_artifact_under_its_own_root(
    llm: InstallResponses, tmp_path: Path
) -> None:
    root = tmp_path / "runs" / RUN_ID
    llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])

    assert walk(a_run(root), "ingest", "decompose") is None

    for path in (TRANSCRIPT, IDEA, BRIEF, RESEARCH, PRD, ISSUES_JSON):
        assert (root / path).is_file(), path
    assert not (tmp_path / "outputs").exists()
    assert not (tmp_path / "inputs").exists()


def test_the_walk_stamps_the_run_id_it_was_given_into_the_issues(
    llm: InstallResponses, tmp_path: Path
) -> None:
    llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])

    walk(a_run(tmp_path), "ingest", "decompose")

    issues = json.loads((tmp_path / ISSUES_JSON).read_text(encoding="utf-8"))
    assert issues["run_id"] == RUN_ID
    assert f"run_id: {RUN_ID}" in (tmp_path / TRANSCRIPT).read_text(encoding="utf-8")


def test_every_stage_reports_itself_once_and_in_order(
    llm: InstallResponses, tmp_path: Path
) -> None:
    llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])
    seen: list[str] = []

    walk(a_run(tmp_path), "ingest", "decompose", lambda stage: seen.append(stage.name))

    assert seen == ["ingest", "intake", "brief", "research", "prd", "decompose"]


def test_a_walk_that_reaches_publish_puts_the_cards_on_the_board(
    llm: InstallResponses, board: FakeBoard, tmp_path: Path
) -> None:
    llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])
    issues = real_issues()

    assert walk(a_run(tmp_path), "ingest", "publish") is None

    made = board.posted("/1/cards")
    assert len(made) == len(issues.issues) + len(issues.deferred)
    assert (tmp_path / "outputs/publish.json").is_file()


def test_a_walk_stops_at_the_stage_it_was_told_to_stop_at(
    llm: InstallResponses, tmp_path: Path
) -> None:
    requests = llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK)])

    walk(a_run(tmp_path), "ingest", "brief")

    assert len(requests) == 2
    assert not (tmp_path / PRD).exists()


def test_candidates_stop_the_walk_and_name_the_file_that_needs_a_person(
    llm: InstallResponses, tmp_path: Path
) -> None:
    requests = llm([ok(CANDIDATES_BLOCK), ok(BRIEF_BLOCK)])

    assert walk(a_run(tmp_path), "ingest", "decompose") == CANDIDATES

    assert len(requests) == 1
    assert not (tmp_path / BRIEF).exists()


def test_research_keeps_the_file_it_finds_and_writes_one_when_it_does_not(
    llm: InstallResponses, tmp_path: Path
) -> None:
    (tmp_path / "outputs").mkdir()
    (tmp_path / RESEARCH).write_text("Настоящий ресёрч.\n", encoding="utf-8")
    llm([ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])
    (tmp_path / BRIEF).write_text("# Бриф\n", encoding="utf-8")

    walk(a_run(tmp_path), "research", "decompose")

    assert (tmp_path / RESEARCH).read_text(encoding="utf-8") == "Настоящий ресёрч.\n"


def test_a_failed_stage_leaves_its_raw_answer_under_the_run_root(
    llm: InstallResponses, tmp_path: Path
) -> None:
    root = tmp_path / "runs" / RUN_ID
    llm([ok("Ответ без единого файла.")] * 2)

    with pytest.raises(Exception):
        walk(a_run(root), "ingest", "intake")

    assert "Ответ без единого файла." in (root / "outputs/intake.raw.md").read_text("utf-8")


def test_missing_before_asks_only_for_what_the_walk_will_not_write(tmp_path: Path) -> None:
    (tmp_path / "outputs").mkdir()

    assert missing_before(tmp_path, "research", "decompose") == [BRIEF]

    (tmp_path / BRIEF).write_text("# Бриф\n", encoding="utf-8")
    assert missing_before(tmp_path, "research", "decompose") == []


def test_a_walk_from_the_first_stage_needs_nothing_on_disk(tmp_path: Path) -> None:
    assert missing_before(tmp_path, "ingest", "publish") == []


def test_publish_declares_no_artifact_because_it_writes_its_own_journal() -> None:
    publish_stage = stage_named("publish")

    assert publish_stage.outputs == (frozenset(),)
    assert publish_stage.inputs == (ISSUES_JSON,)
