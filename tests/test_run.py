import json
from pathlib import Path

import pytest

from app.dialog import Turn
from app.ingest import Source, run_id_of
from app.pipeline import (
    ASSIGNMENT_JSON,
    BRIEF,
    BRIEF_QUESTION,
    CANDIDATES,
    IDEA,
    ISSUES_JSON,
    ISSUES_MD,
    PRD,
    RESEARCH,
    REVIEW_JSON,
    REVIEW_MD,
    STEPS_JSON,
    STEPS_MD,
    TRANSCRIPT,
    stage_named,
)
from app import run as walking
from app.run import (
    ConsentMissing,
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
from app.steps import Assignment, Steps
from app.transcribe import Transcription
from tests.helpers import (
    FIXTURES,
    FakeBoard,
    InstallResponses,
    ok,
    real_issues,
    request_body,
    steps_answer,
)
from tests.test_review import MEETING_DE
from tests.test_stages import said_verbatim
from tests.test_cli import (
    BRIEF_BLOCK,
    CANDIDATES_BLOCK,
    IDEA_BLOCK,
    ISSUES_BLOCKS,
    PRD_BLOCK,
    QUESTION_BLOCK,
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

    Дефолт `Run` повторён нарочно: с обратным тест молча уезжал бы в режим без ворот и проходил
    ворота, которые собирался проверить.
    """
    return Run(
        root=root, run_id=RUN_ID, lang="ru", text=text, source="text", auto_approve=auto_approve
    )


def an_auto_approved_run(root: Path, text: str = TEXT) -> Run:
    return a_run(root, text, auto_approve=True)


def an_asking_run(root: Path) -> Run:
    """Прогон, у которого есть кому отвечать: только такому стадия задаёт вопросы (SPEC §3.3)."""
    return Run(root=root, run_id=RUN_ID, lang="ru", text=TEXT, source="text", interactive=True)


def an_answer(written: str, turns: tuple[Turn, ...] = (), closing: bool = False) -> Redo:
    return Redo(
        kind="answer",
        user_edit=written,
        artifact=BRIEF_QUESTION,
        turns=turns,
        closes_branch=BRIEF_QUESTION if closing else None,
    )


def a_choice(written: str) -> Redo:
    """Ответ на выбор идеи: список стадия отдать уже не вправе, его и закрывает повтор."""
    return Redo(
        kind="choice", user_edit=written, artifact=CANDIDATES, closes_branch=CANDIDATES
    )


def transcribed(run: Run) -> Run:
    """Прогон с расшифровкой на диске: путь до карточек начинается с intake (SPEC §7.2)."""
    walk(run, "ingest", "ingest")
    return run


def a_voice_run(root: Path, monkeypatch: pytest.MonkeyPatch, lang: str = "ru") -> Run:
    """Прогон с голосовым. Расшифровка подменена: её собственные тесты — в test_transcribe."""
    monkeypatch.setattr(
        walking,
        "transcribe",
        lambda audio, run_id: Transcription(text=TEXT, lang=lang, duration_seconds=47),
    )
    return Run(
        root=root,
        run_id=RUN_ID,
        lang="de",
        audio=root / "inputs/voice.oga",
        source="voice",
        auto_approve=True,
    )


def a_file_run(root: Path, consent_confirmed: bool | None) -> Run:
    return Run(
        root=root,
        run_id=RUN_ID,
        lang="de",
        audio=root / "inputs/recording.mp3",
        source="file",
        consent_confirmed=consent_confirmed,
        auto_approve=True,
    )


def test_a_voice_run_writes_a_transcript_that_names_the_source_and_the_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = ingest_body(a_voice_run(tmp_path, monkeypatch))[TRANSCRIPT]

    assert run_id_of(transcript) == RUN_ID
    assert "source: voice\nduration: 47\nlang: ru\n" in transcript
    assert TEXT in transcript


@pytest.mark.parametrize("consent", [None, False])
def test_ingest_refuses_file_without_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consent: bool | None
) -> None:
    """Даже `auto_approve` согласия не снимает, поэтому стадия не верит одной кнопке в боте."""

    def never(audio: Path, run_id: str) -> Transcription:
        raise AssertionError("запись ушла на расшифровку без согласия")

    monkeypatch.setattr(walking, "transcribe", never)

    with pytest.raises(ConsentMissing):
        ingest_body(a_file_run(tmp_path, consent))


def test_file_transcript_carries_consent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        walking,
        "transcribe",
        lambda audio, run_id: Transcription(text=TEXT, lang="ru", duration_seconds=600),
    )

    transcript = ingest_body(a_file_run(tmp_path, True))[TRANSCRIPT]

    assert "source: file\nduration: 600\nlang: ru\nconsent_confirmed: true\n" in transcript


def test_text_and_voice_transcripts_have_no_consent_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Вопроса о согласии им не задавали, и строка с null врала бы, что задавали."""
    voice = ingest_body(a_voice_run(tmp_path, monkeypatch))[TRANSCRIPT]
    text = ingest_body(a_run(tmp_path))[TRANSCRIPT]

    assert "consent_confirmed" not in voice
    assert "consent_confirmed" not in text


def test_the_language_of_the_run_follows_the_voice_and_not_the_default(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DEFAULT_LANG у бота стоит до расшифровки; дальше язык артефактов называет Whisper."""
    requests = llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK)])

    walk(transcribed(a_voice_run(tmp_path, monkeypatch)), "intake", "brief")

    brief_message = request_body(requests[1])["messages"][0]["content"]
    params = brief_message.split("<params>\n", 1)[1].split("\n</params>", 1)[0]
    assert "lang: ru" in params.splitlines()


def test_a_full_walk_writes_every_artifact_under_its_own_root(
    llm: InstallResponses, tmp_path: Path
) -> None:
    root = tmp_path / "runs" / RUN_ID
    llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])

    assert walk(transcribed(an_auto_approved_run(root)), "intake", "decompose") is None

    for path in (TRANSCRIPT, IDEA, BRIEF, RESEARCH, PRD, ISSUES_JSON):
        assert (root / path).is_file(), path
    assert not (tmp_path / "outputs").exists()
    assert not (tmp_path / "inputs").exists()


def test_the_walk_stamps_the_run_id_it_was_given_into_the_issues(
    llm: InstallResponses, tmp_path: Path
) -> None:
    llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])

    walk(transcribed(an_auto_approved_run(tmp_path)), "intake", "decompose")

    issues = json.loads((tmp_path / ISSUES_JSON).read_text(encoding="utf-8"))
    assert issues["run_id"] == RUN_ID
    assert run_id_of((tmp_path / TRANSCRIPT).read_text(encoding="utf-8")) == RUN_ID


def test_every_stage_reports_itself_once_and_in_order(
    llm: InstallResponses, tmp_path: Path
) -> None:
    llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])
    seen: list[str] = []

    walk(
        transcribed(an_auto_approved_run(tmp_path)),
        "intake",
        "decompose",
        lambda stage: seen.append(stage.name),
    )

    assert seen == ["intake", "brief", "research", "prd", "decompose"]


def test_a_walk_that_reaches_publish_puts_the_cards_on_the_board(
    llm: InstallResponses, board: FakeBoard, tmp_path: Path
) -> None:
    llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])
    issues = real_issues()

    assert walk(transcribed(an_auto_approved_run(tmp_path)), "intake", "publish") is None

    made = board.posted("/1/cards")
    assert len(made) == len(issues.issues) + len(issues.deferred)
    assert (tmp_path / "outputs/publish.json").is_file()


def test_a_walk_stops_at_the_stage_it_was_told_to_stop_at(
    llm: InstallResponses, tmp_path: Path
) -> None:
    requests = llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK)])

    walk(transcribed(a_run(tmp_path)), "intake", "brief")

    assert len(requests) == 2
    assert not (tmp_path / PRD).exists()


def test_candidates_stop_the_walk_and_name_the_file_that_needs_a_person(
    llm: InstallResponses, tmp_path: Path
) -> None:
    requests = llm([ok(CANDIDATES_BLOCK), ok(BRIEF_BLOCK)])

    assert walk(transcribed(a_run(tmp_path)), "intake", "decompose") == Pause(
        stage="intake", artifact=CANDIDATES, kind="choice"
    )

    assert len(requests) == 1
    assert not (tmp_path / BRIEF).exists()


def test_a_redo_reaches_the_stage_it_was_meant_for_and_no_other(
    llm: InstallResponses, tmp_path: Path
) -> None:
    """Правка была про артефакт стартовой стадии: brief её получить не должен."""
    requests = llm([ok(CANDIDATES_BLOCK), ok(IDEA_BLOCK), ok(BRIEF_BLOCK)])
    run = transcribed(a_run(tmp_path))
    walk(run, "intake", "decompose")

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
    run = transcribed(a_run(tmp_path))
    walk(run, "intake", "decompose")

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
    run = transcribed(a_run(tmp_path))
    walk(run, "intake", "decompose")

    picked = a_choice("Первую")

    assert walk(run, "intake", "intake", redo=picked) is None

    claim = request_body(requests[2])["messages"][-1]["content"]
    assert "допустим только inputs/idea.md" in claim
    assert (tmp_path / IDEA).exists()


def test_a_stage_that_asks_again_after_a_choice_falls_instead_of_stopping_twice(
    llm: InstallResponses, tmp_path: Path
) -> None:
    """Единственный выход повтора — idea.md: не отдав её и после ремонта, стадия падает."""
    llm([ok(CANDIDATES_BLOCK), ok(CANDIDATES_BLOCK), ok(CANDIDATES_BLOCK)])
    run = transcribed(a_run(tmp_path))
    walk(run, "intake", "decompose")

    picked = a_choice("Первую")

    with pytest.raises(StageError, match="expected inputs/idea.md, got outputs/candidates.md"):
        walk(run, "intake", "intake", redo=picked)


def test_a_choice_redo_may_not_ask_the_same_question_again() -> None:
    assert outputs_after(stage_named("intake"), a_choice("Первую")) == (frozenset({IDEA}),)


def test_a_gate_redo_hands_back_the_same_artifact_because_that_is_the_point() -> None:
    """На воротах правят сам артефакт: тот же запрет закрыл бы стадию наглухо."""
    fixed = Redo(kind="gate", user_edit="Заголовок другой", artifact=BRIEF)

    assert outputs_after(stage_named("brief"), fixed) == stage_named("brief").outputs


def test_an_edit_at_a_gate_runs_the_same_stage_again_and_stops_there_again(
    llm: InstallResponses, tmp_path: Path
) -> None:
    """Правка возвращается в стадию ворот: человек читает переделанное и решает заново."""
    edited = BRIEF_BLOCK.replace("# Brief", "# Brief без пятого раздела")
    requests = llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(edited)])
    run = transcribed(a_run(tmp_path))
    walk(run, "intake", "decompose")

    fixed = Redo(kind="gate", user_edit="Убери пятый раздел", artifact=BRIEF)

    assert walk(run, "brief", "decompose", redo=fixed) == Pause(
        stage="brief", artifact=BRIEF, kind="gate"
    )

    asked = request_body(requests[2])["messages"]
    assert asked[0]["role"] == "assistant"
    assert "<user_edit>\nУбери пятый раздел\n</user_edit>" in asked[-1]["content"]
    assert "# Brief без пятого раздела" in read_artifact(tmp_path, BRIEF)


def test_a_gate_after_brief_stops_a_run_that_was_not_auto_approved(
    llm: InstallResponses, tmp_path: Path
) -> None:
    requests = llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK)])

    assert walk(transcribed(a_run(tmp_path)), "intake", "decompose") == Pause(
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

    assert walk(transcribed(an_auto_approved_run(tmp_path)), "intake", "publish") is None

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

    waiting = walk(transcribed(an_auto_approved_run(tmp_path)), "intake", "decompose")

    assert waiting is not None and waiting.kind == "choice"


def test_a_gate_on_the_last_stage_of_the_walk_lets_the_run_finish(
    llm: InstallResponses, tmp_path: Path
) -> None:
    """Ворота останавливают перед следующей стадией; когда её нет, обход и так кончился."""
    llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK)])

    assert walk(transcribed(a_run(tmp_path)), "intake", "brief") is None

    assert (tmp_path / BRIEF).is_file()


def test_research_keeps_the_file_it_finds_and_writes_one_when_it_does_not(
    llm: InstallResponses, tmp_path: Path
) -> None:
    (tmp_path / "outputs").mkdir()
    (tmp_path / RESEARCH).write_text("Настоящий ресёрч.\n", encoding="utf-8")
    llm([ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])
    (tmp_path / BRIEF).write_text("# Бриф\n", encoding="utf-8")

    walk(an_auto_approved_run(tmp_path), "research", "decompose")

    assert (tmp_path / RESEARCH).read_text(encoding="utf-8") == "Настоящий ресёрч.\n"


def test_a_failed_stage_leaves_its_raw_answer_under_the_run_root(
    llm: InstallResponses, tmp_path: Path
) -> None:
    root = tmp_path / "runs" / RUN_ID
    llm([ok("Ответ без единого файла.")] * 2)

    with pytest.raises(Exception):
        walk(transcribed(a_run(root)), "intake", "intake")

    assert "Ответ без единого файла." in (root / "outputs/intake.raw.md").read_text("utf-8")


def test_missing_before_asks_only_for_what_the_walk_will_not_write(tmp_path: Path) -> None:
    (tmp_path / "outputs").mkdir()

    assert missing_before(tmp_path, "research", "decompose") == [BRIEF]

    (tmp_path / BRIEF).write_text("# Бриф\n", encoding="utf-8")
    assert missing_before(tmp_path, "research", "decompose") == []


def test_a_walk_from_the_first_stage_needs_nothing_on_disk(tmp_path: Path) -> None:
    assert missing_before(tmp_path, "ingest", "review") == []


def test_publish_declares_no_artifact_because_it_writes_its_own_journal() -> None:
    publish_stage = stage_named("publish")

    assert publish_stage.outputs == (frozenset(),)
    assert publish_stage.inputs == (ISSUES_JSON,)


def test_a_question_from_brief_stops_the_walk_and_names_the_file_that_needs_a_person(
    llm: InstallResponses, tmp_path: Path
) -> None:
    requests = llm([ok(IDEA_BLOCK), ok(QUESTION_BLOCK)])

    waiting = walk(transcribed(an_asking_run(tmp_path)), "intake", "decompose")

    assert waiting == Pause(stage="brief", artifact=BRIEF_QUESTION, kind="answer")
    assert (tmp_path / BRIEF_QUESTION).is_file()
    assert not (tmp_path / BRIEF).exists()
    assert len(requests) == 2


def test_brief_hears_whether_anyone_will_answer_its_questions(
    llm: InstallResponses, tmp_path: Path
) -> None:
    """Флаг — свойство прогона, а не записи стадии: у локального отвечать некому (CLAUDE.md §1)."""
    requests = llm([ok(IDEA_BLOCK), ok(QUESTION_BLOCK), ok(IDEA_BLOCK), ok(BRIEF_BLOCK)])

    walk(transcribed(an_asking_run(tmp_path)), "intake", "brief")
    walk(transcribed(a_run(tmp_path)), "intake", "brief")

    assert "interactive: true" in request_body(requests[1])["messages"][-1]["content"]
    assert "interactive: false" in request_body(requests[3])["messages"][-1]["content"]


def test_an_answer_returns_to_brief_and_the_walk_goes_on_to_the_gate(
    llm: InstallResponses, tmp_path: Path
) -> None:
    llm([ok(IDEA_BLOCK), ok(QUESTION_BLOCK), ok(BRIEF_BLOCK)])
    run = transcribed(an_asking_run(tmp_path))
    walk(run, "intake", "decompose")

    waiting = walk(run, "brief", "decompose", redo=an_answer("Пятеро, смотрят каждый день"))

    assert waiting == Pause(stage="brief", artifact=BRIEF, kind="gate")
    assert (tmp_path / BRIEF).is_file()


def test_an_answer_carries_every_earlier_turn_of_the_dialog(
    llm: InstallResponses, tmp_path: Path
) -> None:
    """Без прежних ходов стадия спросит то же самое ещё раз, а ответы человека пропадут."""
    requests = llm([ok(IDEA_BLOCK), ok(QUESTION_BLOCK), ok(BRIEF_BLOCK)])
    run = transcribed(an_asking_run(tmp_path))
    walk(run, "intake", "decompose")

    first = Turn(question="Кто пользователь?\n", answer="Наша же команда")
    walk(run, "brief", "brief", redo=an_answer("Пятеро", turns=(first,)))

    said = request_body(requests[2])["messages"]
    assert [turn["role"] for turn in said] == ["assistant", "user", "assistant", "user"]
    assert first.question in said[0]["content"]
    assert said[1]["content"] == "<user_edit>\nНаша же команда\n</user_edit>"
    assert read_artifact(tmp_path, BRIEF_QUESTION) in said[2]["content"]
    assert "<user_edit>\nПятеро\n</user_edit>" in said[3]["content"]


def test_a_dialog_that_is_over_leaves_the_stage_only_the_brief() -> None:
    assert outputs_after(stage_named("brief"), an_answer("Пятеро", closing=True)) == (
        frozenset({BRIEF}),
    )


def test_an_answer_that_is_not_the_last_one_lets_the_stage_ask_again() -> None:
    assert outputs_after(stage_named("brief"), an_answer("Пятеро")) == stage_named("brief").outputs


def test_a_question_asked_after_the_dialog_is_over_is_repaired_into_the_brief(
    llm: InstallResponses, tmp_path: Path
) -> None:
    """Человек нажал «Собирай»: ещё один вопрос оставил бы его там же, где он был."""
    requests = llm([ok(IDEA_BLOCK), ok(QUESTION_BLOCK), ok(QUESTION_BLOCK), ok(BRIEF_BLOCK)])
    run = transcribed(an_asking_run(tmp_path))
    walk(run, "intake", "decompose")

    assert walk(run, "brief", "brief", redo=an_answer("Хватит", closing=True)) is None

    claim = request_body(requests[3])["messages"][-1]["content"]
    assert "допустим только outputs/brief.md" in claim
    assert (tmp_path / BRIEF).is_file()


def test_a_recording_walks_to_its_review_and_no_further(
    llm: InstallResponses, tmp_path: Path
) -> None:
    """Текст встречи тем же путём, что запись: стадии не знают, откуда пришли данные."""
    spoken = MEETING_DE.split("---\n", 2)[2].strip()
    requests = llm([ok(said_verbatim())])
    run = Run(root=tmp_path, run_id=RUN_ID, lang="de", text=spoken, source="text")

    assert walk(run, "ingest", "review") is None

    for path in (TRANSCRIPT, REVIEW_JSON, REVIEW_MD):
        assert (tmp_path / path).is_file(), path
    assert not (tmp_path / IDEA).exists()
    assert len(requests) == 1
    asked = request_body(requests[0])
    params = asked["messages"][0]["content"].split("<params>\n", 1)[1].split("\n</params>", 1)[0]
    assert params.splitlines() == ["owner_lang: ru"]


PARENT_RUN = "3f9c1a7e5b2d8c40"


def a_review_on_disk(root: Path) -> Path:
    (root / "outputs").mkdir(parents=True)
    (root / REVIEW_JSON).write_text(
        (FIXTURES / "review_de.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return root


def a_task_run(root: Path, parent_root: Path, source: Source, auto_approve: bool = True) -> Run:
    return Run(
        root=root,
        run_id=RUN_ID,
        lang="de",
        source=source,
        auto_approve=auto_approve,
        parent_root=parent_root,
        parent_run_id=PARENT_RUN,
        assignment=1,
    )


def test_an_assignment_walk_writes_the_task_and_its_stamped_steps(
    llm: InstallResponses, tmp_path: Path
) -> None:
    parent = a_review_on_disk(tmp_path / "parent")
    child = tmp_path / "child"
    llm([ok(steps_answer())])

    assert walk(a_task_run(child, parent, "voice"), "assignment", "steps") is None

    assignment = Assignment.model_validate_json(read_artifact(child, ASSIGNMENT_JSON))
    assert (assignment.run_id, assignment.parent_run_id, assignment.number) == (
        RUN_ID,
        PARENT_RUN,
        1,
    )
    assert assignment.meeting_lang == "de"
    steps = Steps.model_validate_json(read_artifact(child, STEPS_JSON))
    assert steps.run_id == RUN_ID and steps.task is not None
    assert (child / STEPS_MD).is_file()


def test_a_text_parent_gives_the_steps_no_meeting_language(
    llm: InstallResponses, tmp_path: Path
) -> None:
    """У текста `lang` это DEFAULT_LANG, а не распознанный язык встречи."""
    parent = a_review_on_disk(tmp_path / "parent")
    requests = llm([ok(steps_answer())])

    walk(a_task_run(tmp_path / "child", parent, "text"), "assignment", "steps")

    assignment = json.loads(read_artifact(tmp_path / "child", ASSIGNMENT_JSON))
    assert assignment["meeting_lang"] is None
    assert '"meeting_lang": null' in request_body(requests[0])["messages"][0]["content"]


def test_an_assignment_without_the_review_of_its_parent_fails_before_the_model(
    llm: InstallResponses, tmp_path: Path
) -> None:
    requests = llm([])

    with pytest.raises(OSError):
        walk(a_task_run(tmp_path / "child", tmp_path / "gone", "voice"), "assignment", "steps")

    assert requests == []


def test_a_task_walk_with_gates_stops_on_the_steps_before_the_card(
    llm: InstallResponses, board: FakeBoard, tmp_path: Path
) -> None:
    parent = a_review_on_disk(tmp_path / "parent")
    llm([ok(steps_answer())])

    waiting = walk(
        a_task_run(tmp_path / "child", parent, "voice", auto_approve=False), "assignment", "card"
    )

    assert waiting == Pause(stage="steps", artifact=STEPS_MD, kind="gate")
    assert board.posted("/1/cards") == []


def test_an_auto_approved_task_walk_ends_with_one_card(
    llm: InstallResponses, board: FakeBoard, tmp_path: Path
) -> None:
    parent = a_review_on_disk(tmp_path / "parent")
    llm([ok(steps_answer())])

    assert walk(a_task_run(tmp_path / "child", parent, "voice"), "assignment", "card") is None

    assert len(board.posted("/1/cards")) == 1
    assert (tmp_path / "child/outputs/publish.json").is_file()
