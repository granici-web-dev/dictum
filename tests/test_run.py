import json
from pathlib import Path

import pytest

from app.pipeline import (
    BRIEF,
    CANDIDATES,
    IDEA,
    ISSUES_JSON,
    ISSUES_MD,
    PRD,
    RESEARCH,
    TRANSCRIPT,
    stage_named,
)
from app import run as walking
from app.run import (
    Pause,
    Redo,
    Run,
    ingest_body,
    missing_before,
    outputs_after,
    read_artifact,
    walk,
)
from app.stages import StageError
from app.transcribe import Transcription
from tests.helpers import FakeBoard, InstallResponses, ok, real_issues, request_body
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


def a_run(root: Path, text: str = TEXT, auto_approve: bool = False) -> Run:
    """Прогон с воротами, как в продакшене: авто-подтверждение тест просит сам.

    Дефолт `Run` повторён нарочно: с обратным тест молча уезжал бы в демо-режим и проходил
    ворота, которые собирался проверить.
    """
    return Run(root=root, run_id=RUN_ID, lang="ru", text=text, auto_approve=auto_approve)


def a_demo_run(root: Path, text: str = TEXT) -> Run:
    return a_run(root, text, auto_approve=True)


def a_voice_run(root: Path, monkeypatch: pytest.MonkeyPatch, lang: str = "ru") -> Run:
    """Прогон с голосовым. Расшифровка подменена: её собственные тесты — в test_transcribe."""
    monkeypatch.setattr(
        walking,
        "transcribe",
        lambda audio: Transcription(text=TEXT, lang=lang, duration_seconds=47),
    )
    return Run(
        root=root,
        run_id=RUN_ID,
        lang="de",
        audio=root / "inputs/voice.oga",
        auto_approve=True,
    )


def test_a_voice_run_writes_a_transcript_that_names_the_source_and_the_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = ingest_body(a_voice_run(tmp_path, monkeypatch))[TRANSCRIPT]

    assert f"run_id: {RUN_ID}\nsource: voice\nduration: 47\nlang: ru\n" in transcript
    assert TEXT in transcript


def test_the_language_of_the_run_follows_the_voice_and_not_the_default(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DEFAULT_LANG у бота стоит до расшифровки; дальше язык артефактов называет Whisper."""
    requests = llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK)])

    walk(a_voice_run(tmp_path, monkeypatch), "ingest", "brief")

    brief_message = request_body(requests[1])["messages"][0]["content"]
    params = brief_message.split("<params>\n", 1)[1].split("\n</params>", 1)[0]
    assert "lang: ru" in params.splitlines()


def test_a_full_walk_writes_every_artifact_under_its_own_root(
    llm: InstallResponses, tmp_path: Path
) -> None:
    root = tmp_path / "runs" / RUN_ID
    llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])

    assert walk(a_demo_run(root), "ingest", "decompose") is None

    for path in (TRANSCRIPT, IDEA, BRIEF, RESEARCH, PRD, ISSUES_JSON):
        assert (root / path).is_file(), path
    assert not (tmp_path / "outputs").exists()
    assert not (tmp_path / "inputs").exists()


def test_the_walk_stamps_the_run_id_it_was_given_into_the_issues(
    llm: InstallResponses, tmp_path: Path
) -> None:
    llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])

    walk(a_demo_run(tmp_path), "ingest", "decompose")

    issues = json.loads((tmp_path / ISSUES_JSON).read_text(encoding="utf-8"))
    assert issues["run_id"] == RUN_ID
    assert f"run_id: {RUN_ID}" in (tmp_path / TRANSCRIPT).read_text(encoding="utf-8")


def test_every_stage_reports_itself_once_and_in_order(
    llm: InstallResponses, tmp_path: Path
) -> None:
    llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])
    seen: list[str] = []

    walk(a_demo_run(tmp_path), "ingest", "decompose", lambda stage: seen.append(stage.name))

    assert seen == ["ingest", "intake", "brief", "research", "prd", "decompose"]


def test_a_walk_that_reaches_publish_puts_the_cards_on_the_board(
    llm: InstallResponses, board: FakeBoard, tmp_path: Path
) -> None:
    llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])
    issues = real_issues()

    assert walk(a_demo_run(tmp_path), "ingest", "publish") is None

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

    assert walk(a_run(tmp_path), "ingest", "decompose") == Pause(
        stage="intake", artifact=CANDIDATES, kind="choice"
    )

    assert len(requests) == 1
    assert not (tmp_path / BRIEF).exists()


def test_a_redo_reaches_the_stage_it_was_meant_for_and_no_other(
    llm: InstallResponses, tmp_path: Path
) -> None:
    """Правка была про артефакт стартовой стадии: brief её получить не должен."""
    requests = llm([ok(CANDIDATES_BLOCK), ok(IDEA_BLOCK), ok(BRIEF_BLOCK)])
    run = a_run(tmp_path)
    walk(run, "ingest", "decompose")

    picked = Redo(kind="choice", user_edit="Выбрана идея 2: Отчёты", artifact=CANDIDATES)
    walk(run, "intake", "brief", redo=picked)

    chosen = request_body(requests[1])["messages"]
    assert "<user_edit>\nВыбрана идея 2: Отчёты\n</user_edit>" in chosen[-1]["content"]
    assert "user_edit" not in request_body(requests[2])["messages"][-1]["content"]


def test_a_redo_carries_the_previous_answer_so_the_stage_does_not_start_over(
    llm: InstallResponses, tmp_path: Path
) -> None:
    """Без своего прошлого ответа стадия соберёт артефакт заново (SPEC §3.2)."""
    requests = llm([ok(CANDIDATES_BLOCK), ok(IDEA_BLOCK)])
    run = a_run(tmp_path)
    walk(run, "ingest", "decompose")

    walk(run, "intake", "intake", redo=Redo(kind="choice", user_edit="Первую", artifact=CANDIDATES))

    history = request_body(requests[1])["messages"][0]
    assert history["role"] == "assistant"
    assert history["content"].startswith('<file path="outputs/candidates.md">')
    assert read_artifact(tmp_path, CANDIDATES) in history["content"]


def test_a_second_list_after_a_choice_is_repaired_into_the_chosen_idea(
    llm: InstallResponses, tmp_path: Path
) -> None:
    """Выбор сделан: тот же список остановил бы прогон на том же месте, а ответ человека пропал."""
    requests = llm([ok(CANDIDATES_BLOCK), ok(CANDIDATES_BLOCK), ok(IDEA_BLOCK)])
    run = a_run(tmp_path)
    walk(run, "ingest", "decompose")

    picked = Redo(kind="choice", user_edit="Первую", artifact=CANDIDATES)

    assert walk(run, "intake", "intake", redo=picked) is None

    claim = request_body(requests[2])["messages"][-1]["content"]
    assert "допустим только inputs/idea.md" in claim
    assert (tmp_path / IDEA).exists()


def test_a_stage_that_asks_again_after_a_choice_falls_instead_of_stopping_twice(
    llm: InstallResponses, tmp_path: Path
) -> None:
    """Единственный выход повтора — idea.md: не отдав её и после ремонта, стадия падает."""
    llm([ok(CANDIDATES_BLOCK), ok(CANDIDATES_BLOCK), ok(CANDIDATES_BLOCK)])
    run = a_run(tmp_path)
    walk(run, "ingest", "decompose")

    picked = Redo(kind="choice", user_edit="Первую", artifact=CANDIDATES)

    with pytest.raises(StageError, match="expected inputs/idea.md, got outputs/candidates.md"):
        walk(run, "intake", "intake", redo=picked)


def test_a_choice_redo_may_not_ask_the_same_question_again() -> None:
    choice = Redo(kind="choice", user_edit="Первую", artifact=CANDIDATES)

    assert outputs_after(stage_named("intake"), choice) == (frozenset({IDEA}),)


def test_a_gate_redo_hands_back_the_same_artifact_because_that_is_the_point() -> None:
    """На воротах правят сам артефакт: тот же запрет закрыл бы стадию наглухо."""
    fixed = Redo(kind="gate", user_edit="Заголовок другой", artifact=BRIEF)

    assert outputs_after(stage_named("brief"), fixed) == stage_named("brief").outputs


def test_a_gate_after_brief_stops_a_run_that_was_not_auto_approved(
    llm: InstallResponses, tmp_path: Path
) -> None:
    requests = llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK)])

    assert walk(a_run(tmp_path), "ingest", "decompose") == Pause(
        stage="brief", artifact=BRIEF, kind="gate"
    )

    assert len(requests) == 2
    assert (tmp_path / BRIEF).is_file()
    assert not (tmp_path / PRD).exists()


def test_an_auto_approved_run_walks_past_every_gate(
    llm: InstallResponses, board: FakeBoard, tmp_path: Path
) -> None:
    """До publish, а не до decompose: иначе последние ворота гасит конец обхода, а не флаг."""
    llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])

    assert walk(a_demo_run(tmp_path), "ingest", "publish") is None

    assert (tmp_path / "outputs/publish.json").is_file()


def test_a_gate_after_decompose_holds_the_backlog_back_from_the_board(
    llm: InstallResponses, board: FakeBoard, tmp_path: Path
) -> None:
    llm([ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])
    (tmp_path / "outputs").mkdir()
    (tmp_path / BRIEF).write_text("# Бриф\n", encoding="utf-8")

    assert walk(a_run(tmp_path), "research", "publish") == Pause(
        stage="decompose", artifact=ISSUES_MD, kind="gate"
    )

    assert (tmp_path / ISSUES_MD).is_file()
    assert board.posted("/1/cards") == []


def test_candidates_stop_even_an_auto_approved_run(
    llm: InstallResponses, tmp_path: Path
) -> None:
    """Выбор — не ворота: подтверждать нечего, идею из нескольких выбирает человек."""
    llm([ok(CANDIDATES_BLOCK), ok(BRIEF_BLOCK)])

    waiting = walk(a_demo_run(tmp_path), "ingest", "decompose")

    assert waiting is not None and waiting.kind == "choice"


def test_a_gate_on_the_last_stage_of_the_walk_lets_the_run_finish(
    llm: InstallResponses, tmp_path: Path
) -> None:
    """Ворота останавливают перед следующей стадией; когда её нет, обход и так кончился."""
    llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK)])

    assert walk(a_run(tmp_path), "ingest", "brief") is None

    assert (tmp_path / BRIEF).is_file()


def test_research_keeps_the_file_it_finds_and_writes_one_when_it_does_not(
    llm: InstallResponses, tmp_path: Path
) -> None:
    (tmp_path / "outputs").mkdir()
    (tmp_path / RESEARCH).write_text("Настоящий ресёрч.\n", encoding="utf-8")
    llm([ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])
    (tmp_path / BRIEF).write_text("# Бриф\n", encoding="utf-8")

    walk(a_demo_run(tmp_path), "research", "decompose")

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
