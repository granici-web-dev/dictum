import json
from typing import Any
import logging
from pathlib import Path

import pytest

from app.cli import (
    EXIT_NEEDS_A_DECISION,
    EXIT_OK,
    EXIT_STAGE_FAILED,
    EXIT_USAGE,
    main,
)
from app import bot, cli, publish, stages
from app.config import settings
from app.ingest import build_transcript, run_id_of
from app.answers import read_answers
from app.pipeline import (
    ANSWERS,
    APPROACH_JSON,
    ASSIGNMENT_JSON,
    BRIEF,
    CANDIDATES,
    CLARIFY_JSON,
    IDEA,
    PRD,
    PROJECT,
    RESEARCH,
    REVIEW_JSON,
    REVIEW_MD,
    STEPS_JSON,
    STEPS_MD,
    TRANSCRIPT,
)
from tests.helpers import (
    BROKEN_ISSUES,
    FIXTURES,
    CLARIFY_DE,
    InstallResponses,
    APPROACH_NEW_DE,
    approach_answer,
    clarify_answer,
    decompose_answer,
    ok,
    searched,
    request_body,
    server_error,
    steps_answer,
)

EARLIER_RUN = "прогон-восемь"


def transcript_of_an_earlier_run(root: Path) -> Path:
    path = root / TRANSCRIPT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        build_transcript("Идея с прошлого прогона.", "ru", EARLIER_RUN, "text", None, None), "utf-8"
    )
    return path


IDEA_BLOCK = (
    '<file path="inputs/idea.md">\n---\nsource: text\nlang: ru\nconfidence: high\n---\n\n'
    "# Напоминания о дедлайнах\n\n## Summary\nБот присылает список карточек.\n</file>"
)
CANDIDATES_BLOCK = (
    '<file path="outputs/candidates.md">\n---\noutcome: multiple\n---\n\n'
    "# Ideas found: 2\n\n"
    "1. **Напоминания** — бот шлёт список.\n2. **Отчёты** — сводка за неделю.\n</file>"
)
BRIEF_BLOCK = (
    '<file path="outputs/brief.md">\n---\nname: Напоминания\nkind: product\nlang: ru\n'
    "open_questions: 2\n---\n\n# Brief\n\n## 1. Users\n[уточнить: размер команды]\n</file>"
)
QUESTION_BLOCK = (
    '<file path="outputs/brief_question.md">\n'
    "Сколько человек в команде и как часто они смотрят в Trello?\n</file>"
)
PRD_BLOCK = (
    '<file path="outputs/prd.md">\n# PRD: Напоминания\n\n## MVP scope\n'
    "| ID | Scenario / screen | Priority | Depends on | Done when |\n"
    "|---|---|---|---|---|\n| S1 | Утренний список | Must | — | Список пришёл в 9:00 |\n</file>"
)
ISSUES_BLOCKS = decompose_answer()
TEXT = "Хочу, чтобы бот напоминал о дедлайнах в Trello"
# Разбор TEXT: цитата взята из него дословно, иначе сверка потребовала бы ремонтного повтора.
# Язык называет разбор: у текста его не распознавал никто, и без него проверка требует ремонта.
REVIEW_BLOCK = (
    '<file path="outputs/review.json">\n'
    + json.dumps(
        {
            "meeting_lang": "ru",
            "tasks": [
                {
                    "title": "Сделать напоминания о дедлайнах",
                    "summary": "Бот напоминает о дедлайнах карточек в Trello.",
                    "assigned_by": "[уточнить: кто поручил]",
                    "assignee": "[уточнить: кому]",
                    "status": "decision",
                    "deadline": None,
                    "constraints": [],
                    "do_not": [],
                    "ask_back": [],
                    "quotes": [{"original": TEXT, "translation": None}],
                }
            ],
            "topics": [],
        },
        ensure_ascii=False,
    )
    + "\n</file>"
)
PRD_FROM_A_REAL_RUN = (Path(__file__).parent.parent / "fixtures/prd_real.md").read_text(
    encoding="utf-8"
)


def test_run_text_ends_with_the_review_and_names_where_it_lies(
    llm: InstallResponses,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.chdir(tmp_path)
    requests = llm([ok(REVIEW_BLOCK)])

    with caplog.at_level(logging.INFO, logger="app.cli"):
        assert main([TEXT, "--lang", "ru"]) == EXIT_OK

    written = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert written == [TRANSCRIPT, REVIEW_JSON, REVIEW_MD]
    assert len(requests) == 1
    assert REVIEW_MD in caplog.text


def test_run_text_from_intake_writes_the_artifact_of_every_stage_up_to_the_backlog(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    transcript_of_an_earlier_run(tmp_path)
    llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])

    assert main(["--from", "intake"]) == EXIT_OK

    written = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert written == [
        "inputs/idea.md",
        "inputs/transcript.md",
        "outputs/brief.md",
        "outputs/issues.json",
        "outputs/issues.md",
        "outputs/prd.md",
        "outputs/research.md",
    ]


def test_run_from_review_repeats_the_review_over_the_transcript_on_disk(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    transcript_of_an_earlier_run(tmp_path)
    answer = REVIEW_BLOCK.replace(TEXT, "Идея с прошлого прогона.")
    requests = llm([ok(answer)])

    assert main(["--from", "review"]) == EXIT_OK

    assert len(requests) == 1
    assert (tmp_path / REVIEW_MD).is_file()
    assert not (tmp_path / IDEA).exists()


def test_run_from_review_without_a_transcript_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        main(["--from", "review"])

    assert exit_info.value.code == EXIT_USAGE


def test_run_text_writes_the_transcript_frontmatter(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    llm([ok(REVIEW_BLOCK)])

    main([TEXT, "--lang", "ru"])

    transcript = (tmp_path / TRANSCRIPT).read_text(encoding="utf-8")
    assert "\nsource: text\nduration: null\nlang: ru\n---\n" in transcript
    assert run_id_of(transcript)
    assert transcript.endswith(TEXT + "\n")


def test_run_text_reads_the_input_from_a_file(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    idea = tmp_path / "idea.txt"
    idea.write_text(TEXT, encoding="utf-8")
    llm([ok(REVIEW_BLOCK)])

    assert main(["--file", str(idea), "--lang", "ru"]) == EXIT_OK
    assert TEXT in (tmp_path / "inputs/transcript.md").read_text(encoding="utf-8")


def test_run_text_tells_brief_that_nobody_will_answer(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    transcript_of_an_earlier_run(tmp_path)
    requests = llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])

    main(["--from", "intake"])

    brief_message = request_body(requests[1])["messages"][0]["content"]
    # Только блок <params>: «lang: ru» стоит ещё и во frontmatter самого idea.md, который едет
    # тем же сообщением, и проверка по всему тексту проходила бы с пустыми параметрами.
    params = brief_message.split("<params>\n", 1)[1].split("\n</params>", 1)[0]
    assert params.splitlines() == ["mode: batch", "lang: ru", "interactive: false"]


def test_run_text_feeds_prd_the_brief_research_and_template(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    transcript_of_an_earlier_run(tmp_path)
    requests = llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])

    main(["--from", "intake"])

    prd_message = request_body(requests[2])["messages"][0]["content"]
    assert '<file path="outputs/brief.md">' in prd_message
    assert "Ресёрч не запускался" in prd_message
    assert "MVP scope" in prd_message


def test_run_text_stops_when_intake_returns_candidates(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    transcript_of_an_earlier_run(tmp_path)
    requests = llm([ok(CANDIDATES_BLOCK)])

    assert main(["--from", "intake"]) == EXIT_NEEDS_A_DECISION

    assert len(requests) == 1
    assert (tmp_path / CANDIDATES).exists()
    assert not (tmp_path / BRIEF).exists()


def test_gates_stop_the_local_run_after_brief_and_name_the_stage_to_resume_from(
    llm: InstallResponses,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.chdir(tmp_path)
    transcript_of_an_earlier_run(tmp_path)
    requests = llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK)])

    with caplog.at_level(logging.INFO, logger="app.cli"):
        assert main(["--from", "intake", "--gates"]) == EXIT_NEEDS_A_DECISION

    assert len(requests) == 2
    assert (tmp_path / BRIEF).is_file()
    assert not (tmp_path / PRD).exists()
    assert "--from research" in caplog.text


def test_run_text_saves_the_raw_answer_of_a_failed_stage(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    empty = '<file path="outputs/issues.json">\n{"issues": []}\n</file>'
    transcript_of_an_earlier_run(tmp_path)
    llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(empty), ok(empty)])

    assert main(["--from", "intake"]) == EXIT_STAGE_FAILED

    assert (tmp_path / "outputs/decompose.raw.md").read_text(encoding="utf-8") == empty
    assert not (tmp_path / "outputs/issues.json").exists()


def test_a_typo_is_not_mistaken_for_a_pipeline_outcome() -> None:
    for argv in ([], [""], ["--lng", "ru", TEXT], ["--file", "/nope/nope.md"]):
        with pytest.raises(SystemExit) as exit_info:
            main(argv)
        assert exit_info.value.code == EXIT_USAGE


def test_run_text_reports_a_missing_key_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    stages.anthropic_client.cache_clear()

    assert main([TEXT, "--lang", "ru"]) == EXIT_STAGE_FAILED


def test_run_text_reports_an_api_error_without_a_traceback(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    llm([server_error(), server_error(), server_error()])

    assert main([TEXT, "--lang", "ru"]) == EXIT_STAGE_FAILED


def test_a_continued_run_keeps_the_language_of_its_transcript(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """С P3-01 транскрипт несёт язык от Whisper, а не всегдашний DEFAULT_LANG.

    Продолженный прогон обязан взять его оттуда: иначе по русской идее соберётся немецкий бриф.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "default_lang", "de")
    (tmp_path / "inputs").mkdir()
    (tmp_path / TRANSCRIPT).write_text(
        build_transcript("Хочу бота.", "ru", EARLIER_RUN, "voice", 34, None), encoding="utf-8"
    )
    (tmp_path / IDEA).write_text("# Идея\n", encoding="utf-8")
    requests = llm([ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])

    assert main(["--from", "brief"]) == EXIT_OK

    brief_message = request_body(requests[0])["messages"][0]["content"]
    params = brief_message.split("<params>\n", 1)[1].split("\n</params>", 1)[0]
    assert "lang: ru" in params.splitlines()


def test_run_text_takes_the_language_from_the_input_file(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    idea = tmp_path / "idea.md"
    idea.write_text(f"---\nsource: text\nlang: ru\n---\n\n{TEXT}\n", encoding="utf-8")
    llm([ok(REVIEW_BLOCK)])

    assert main(["--file", str(idea)]) == EXIT_OK

    transcript = (tmp_path / "inputs/transcript.md").read_text(encoding="utf-8")
    assert transcript.count("---") == 2
    assert "lang: ru" in transcript
    assert "lang: de" not in transcript


def test_run_text_falls_back_to_the_configured_language(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    llm([ok(REVIEW_BLOCK)])

    main([TEXT])

    transcript = (tmp_path / "inputs/transcript.md").read_text(encoding="utf-8")
    assert f"lang: {settings.default_lang}" in transcript


def test_run_from_prd_uses_the_artifacts_already_on_disk(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "outputs").mkdir()
    (tmp_path / BRIEF).write_text("# Бриф с прошлого прогона\n", encoding="utf-8")
    (tmp_path / RESEARCH).write_text("Ресёрч с прошлого прогона.\n", encoding="utf-8")
    transcript = transcript_of_an_earlier_run(tmp_path)
    was = transcript.read_text(encoding="utf-8")
    requests = llm([ok(PRD_FROM_A_REAL_RUN), ok(ISSUES_BLOCKS)])

    assert main(["--from", "prd"]) == EXIT_OK

    assert len(requests) == 2
    assert "# Бриф с прошлого прогона" in request_body(requests[0])["messages"][0]["content"]
    # Заголовок русский: фикстура — записанный PRD прогона, который прошёл до правила о
    # заголовках (CLAUDE.md §4), и переписать его значило бы выдать выдуманный артефакт
    # за настоящий. Проверяется здесь запись файла, а не форма заголовков.
    assert "Скоп MVP" in (tmp_path / PRD).read_text(encoding="utf-8")
    assert transcript.read_text(encoding="utf-8") == was


def test_run_from_decompose_feeds_it_the_prd_from_disk(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "outputs").mkdir()
    (tmp_path / PRD).write_text(PRD_FROM_A_REAL_RUN, encoding="utf-8")
    transcript_of_an_earlier_run(tmp_path)
    requests = llm([ok(ISSUES_BLOCKS)])

    assert main(["--from", "decompose"]) == EXIT_OK

    assert len(requests) == 1
    assert "| S1 |" in request_body(requests[0])["messages"][0]["content"]


def test_run_from_a_stage_without_its_input_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        main(["--from", "prd"])

    assert exit_info.value.code == EXIT_USAGE


def test_from_does_not_take_a_text_as_well(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        main([TEXT, "--from", "prd"])

    assert exit_info.value.code == EXIT_USAGE


def test_run_text_keeps_the_second_attempt_when_decompose_stays_invalid(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "outputs").mkdir()
    (tmp_path / PRD).write_text(PRD_FROM_A_REAL_RUN, encoding="utf-8")
    transcript_of_an_earlier_run(tmp_path)
    second = BROKEN_ISSUES.replace('"outputs/prd.md"', '"вторая попытка"', 1)
    requests = llm([ok(decompose_answer(BROKEN_ISSUES)), ok(decompose_answer(second))])

    assert main(["--from", "decompose"]) == EXIT_STAGE_FAILED

    assert len(requests) == 2
    assert not (tmp_path / "outputs/issues.json").exists()
    assert not (tmp_path / "outputs/issues.md").exists()
    raw = (tmp_path / "outputs/decompose.raw.md").read_text(encoding="utf-8")
    assert "вторая попытка" in raw


def test_a_run_carries_one_id_from_the_transcript_into_the_issues(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Разбор и путь до карточек — два маршрута одного прогона, и номер у них один."""
    monkeypatch.chdir(tmp_path)
    llm([ok(REVIEW_BLOCK), ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])

    assert main([TEXT, "--lang", "ru"]) == EXIT_OK
    assert main(["--from", "intake"]) == EXIT_OK

    started = run_id_of((tmp_path / TRANSCRIPT).read_text(encoding="utf-8"))
    issues = json.loads((tmp_path / "outputs/issues.json").read_text(encoding="utf-8"))
    assert started
    assert issues["run_id"] == started


def test_a_resumed_run_keeps_the_id_it_started_with(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "outputs").mkdir()
    (tmp_path / PRD).write_text(PRD_FROM_A_REAL_RUN, encoding="utf-8")
    transcript_of_an_earlier_run(tmp_path)
    llm([ok(ISSUES_BLOCKS)])

    assert main(["--from", "decompose"]) == EXIT_OK

    issues = json.loads((tmp_path / "outputs/issues.json").read_text(encoding="utf-8"))
    assert issues["run_id"] == EARLIER_RUN


def test_a_run_older_than_the_rule_is_refused_instead_of_given_a_second_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "outputs").mkdir()
    (tmp_path / PRD).write_text(PRD_FROM_A_REAL_RUN, encoding="utf-8")
    (tmp_path / TRANSCRIPT).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / TRANSCRIPT).write_text("---\nsource: text\nlang: ru\n---\n\nСтарый.\n", "utf-8")

    with pytest.raises(SystemExit) as exit_info:
        main(["--from", "decompose"])

    assert exit_info.value.code == EXIT_USAGE


def test_a_missing_artifact_names_the_stage_that_makes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "outputs").mkdir()
    (tmp_path / BRIEF).write_text("# Бриф\n", encoding="utf-8")
    transcript_of_an_earlier_run(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        main(["--from", "prd"])

    assert exit_info.value.code == EXIT_USAGE
    assert "начните с --from research" in capsys.readouterr().err


def test_the_skipped_research_does_not_overwrite_one_already_on_disk(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "outputs").mkdir()
    (tmp_path / BRIEF).write_text("# Бриф\n", encoding="utf-8")
    (tmp_path / RESEARCH).write_text("Настоящий ресёрч.\n", encoding="utf-8")
    transcript_of_an_earlier_run(tmp_path)
    llm([ok(PRD_FROM_A_REAL_RUN), ok(ISSUES_BLOCKS)])

    assert main(["--from", "research"]) == EXIT_OK

    assert (tmp_path / RESEARCH).read_text(encoding="utf-8") == "Настоящий ресёрч.\n"


def test_research_writes_its_line_when_nothing_is_on_disk(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "outputs").mkdir()
    (tmp_path / BRIEF).write_text("# Бриф\n", encoding="utf-8")
    transcript_of_an_earlier_run(tmp_path)
    requests = llm([ok(PRD_FROM_A_REAL_RUN), ok(ISSUES_BLOCKS)])

    assert main(["--from", "research"]) == EXIT_OK

    assert "Ресёрч не запускался" in (tmp_path / RESEARCH).read_text(encoding="utf-8")
    assert len(requests) == 2


def test_a_walk_is_refused_before_it_writes_anything_it_cannot_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "outputs").mkdir()
    transcript_of_an_earlier_run(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        main(["--from", "research"])

    assert exit_info.value.code == EXIT_USAGE
    # research читать нечего, а вот prd следом за ним упал бы на brief.md, успев записать
    # research.md: проверять только входы стартовой стадии мало.
    assert "нужен outputs/brief.md" in capsys.readouterr().err
    assert not (tmp_path / RESEARCH).exists()


def test_a_walk_does_not_demand_what_it_will_write_itself(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "outputs").mkdir()
    (tmp_path / BRIEF).write_text("# Бриф\n", encoding="utf-8")
    transcript_of_an_earlier_run(tmp_path)
    llm([ok(PRD_FROM_A_REAL_RUN), ok(ISSUES_BLOCKS)])

    # research.md на диске нет, но его кладёт сама стадия research внутри обхода.
    assert main(["--from", "research"]) == EXIT_OK


def test_a_missing_transcript_says_it_is_the_run_id_that_is_wanted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "outputs").mkdir()
    (tmp_path / PRD).write_text(PRD_FROM_A_REAL_RUN, encoding="utf-8")

    with pytest.raises(SystemExit) as exit_info:
        main(["--from", "decompose"])

    assert exit_info.value.code == EXIT_USAGE
    assert "run_id" in capsys.readouterr().err


def test_a_module_run_as_a_command_names_its_logger_by_hand() -> None:
    """`python -m app.cli` даёт __name__ == "__main__", а уровень INFO поднимают дереву "app".

    caplog ловит запись при любом имени логгера, поэтому промах видно только по имени.
    """
    named = [module.logger.name for module in (cli, bot, publish)]

    assert named == ["app.cli", "app.bot", "app.publish"]


MEETING_RUN = "3f9c1a7e5b2d8c40"


def a_review_of_an_earlier_run(root: Path, with_review: bool = True) -> None:
    """Разбор немецкой встречи, лежащий после make run-text: его каталог и есть каталог родителя."""
    (root / "inputs").mkdir(parents=True, exist_ok=True)
    (root / TRANSCRIPT).write_text(
        build_transcript("Erstens das Login-Formular.", "de", MEETING_RUN, "file", 95, True),
        encoding="utf-8",
    )
    if with_review:
        (root / "outputs").mkdir(exist_ok=True)
        (root / REVIEW_JSON).write_text(
            (FIXTURES / "review_de.json").read_text(encoding="utf-8"), encoding="utf-8"
        )


def test_task_writes_the_assignment_under_a_run_id_of_its_own_and_stops_at_the_steps(
    llm: InstallResponses,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.chdir(tmp_path)
    a_review_of_an_earlier_run(tmp_path)
    requests = llm([ok(clarify_answer()), searched(approach_answer()), ok(steps_answer())])

    with caplog.at_level(logging.INFO, logger="app.cli"):
        assert main(["--task", "1"]) == EXIT_OK

    assignment = json.loads((tmp_path / ASSIGNMENT_JSON).read_text(encoding="utf-8"))
    assert assignment["parent_run_id"] == MEETING_RUN
    assert assignment["run_id"] not in ("", MEETING_RUN)
    assert assignment["meeting_lang"] == "de"
    steps = json.loads((tmp_path / STEPS_JSON).read_text(encoding="utf-8"))
    assert steps["run_id"] == assignment["run_id"]
    assert len(requests) == 3
    assert read_answers((tmp_path / ANSWERS).read_text(encoding="utf-8")).status == "not_sent"
    assert steps["unanswered"] == [1, 2, 3, 4]
    assert not (tmp_path / "outputs/publish.json").exists()
    assert STEPS_MD in caplog.text


@pytest.mark.parametrize(
    ("argv", "with_review"),
    [
        (["--task", "9"], True),
        (["--task", "0"], True),
        (["--task", "один"], True),
        (["--task", "1"], False),
        (["--task", "1", "--gates"], True),
        (["--task", "1", "--from", "steps"], True),
        (["--from", "card"], True),
        (["--from", "assignment"], True),
        (["--from", "publish"], True),
        (["--answers", "answers.txt"], True),
        (["--task", "1", "--answers", "answers.txt"], True),
        (["--from", "intake", "--answers", "answers.txt"], True),
    ],
)
def test_a_task_that_cannot_be_walked_is_a_usage_error_before_any_call(
    llm: InstallResponses,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    with_review: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    a_review_of_an_earlier_run(tmp_path, with_review)
    requests = llm([])

    with pytest.raises(SystemExit) as exit_info:
        main(argv)

    assert exit_info.value.code == EXIT_USAGE
    assert requests == []
    assert not (tmp_path / ASSIGNMENT_JSON).exists()


def test_no_research_walks_to_the_steps_without_a_single_paid_search(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    a_review_of_an_earlier_run(tmp_path)
    def only_the_teamlead_questions(data: dict[str, Any]) -> None:
        data["unanswered"] = [1, 3]
        data["approach"] = None

    requests = llm([ok(clarify_answer()), ok(steps_answer(only_the_teamlead_questions))])

    assert main(["--task", "1", "--no-research"]) == EXIT_OK

    assert len(requests) == 2
    approach = json.loads((tmp_path / APPROACH_JSON).read_text(encoding="utf-8"))
    assert approach["status"] == "skipped"


def test_no_research_without_a_task_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        main(["--no-research", "--from", "steps"])

    assert exit_info.value.code == EXIT_USAGE


def a_task_asked_on_disk(root: Path) -> None:
    """Поручение, по которому --task N уже задал вопросы и не отправил их: всё, что читают шаги."""
    (root / "inputs").mkdir(exist_ok=True)
    (root / "outputs").mkdir(exist_ok=True)
    (root / ASSIGNMENT_JSON).write_text(
        (FIXTURES / "assignment_de.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (root / CLARIFY_JSON).write_text(CLARIFY_DE, encoding="utf-8")
    (root / ANSWERS).write_text("---\nstatus: not_sent\n---\n", encoding="utf-8")
    (root / APPROACH_JSON).write_text(APPROACH_NEW_DE, encoding="utf-8")
    (root / PROJECT).write_text(
        '---\nconfigured: false\nsource: null\ntaken_at: "2026-09-17T10:02:00+00:00"\n'
        "stack: false\nprinciples: false\ntesting: false\nsame_as: {}\n---\n",
        encoding="utf-8",
    )


def test_steps_are_repeated_under_the_run_id_of_the_assignment_on_disk(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    a_task_asked_on_disk(tmp_path)
    llm([ok(steps_answer())])

    assert main(["--from", "steps"]) == EXIT_OK

    steps = json.loads((tmp_path / STEPS_JSON).read_text(encoding="utf-8"))
    assert steps["run_id"] == "7b2e0d91c4a3f615"


def test_steps_with_answers_take_the_reply_as_it_was_written_and_run_again(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """answers_de_partial.md составлен вручную: тимлид ответил «zu 1» на второй вопрос бота."""
    monkeypatch.chdir(tmp_path)
    a_task_asked_on_disk(tmp_path)
    reply = read_answers((FIXTURES / "answers_de_partial.md").read_text(encoding="utf-8")).text
    (tmp_path / "reply.txt").write_text(reply + "\n", encoding="utf-8")
    requests = llm([ok(steps_answer())])

    assert main(["--from", "steps", "--answers", "reply.txt"]) == EXIT_OK

    answers = read_answers((tmp_path / ANSWERS).read_text(encoding="utf-8"))
    assert (answers.status, answers.text) == ("answered", reply)
    assert reply in request_body(requests[0])["messages"][0]["content"]
    steps = json.loads((tmp_path / STEPS_JSON).read_text(encoding="utf-8"))
    assert steps["unanswered"] == [1, 3, 4]


@pytest.mark.parametrize("content", [None, "  \n"])
def test_steps_with_an_answers_file_that_is_missing_or_empty_is_a_usage_error(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str | None
) -> None:
    monkeypatch.chdir(tmp_path)
    a_task_asked_on_disk(tmp_path)
    if content is not None:
        (tmp_path / "reply.txt").write_text(content, encoding="utf-8")
    requests = llm([])

    with pytest.raises(SystemExit) as exit_info:
        main(["--from", "steps", "--answers", "reply.txt"])

    assert exit_info.value.code == EXIT_USAGE
    assert requests == []
    assert read_answers((tmp_path / ANSWERS).read_text(encoding="utf-8")).status == "not_sent"


def test_steps_on_an_assignment_older_than_the_questions_is_a_usage_error(
    llm: InstallResponses,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Прогон поручения до вопросов для тимлида повторяется с --task N, а не с --from steps."""
    monkeypatch.chdir(tmp_path)
    a_task_asked_on_disk(tmp_path)
    (tmp_path / CLARIFY_JSON).unlink()
    requests = llm([])

    with pytest.raises(SystemExit) as exit_info:
        main(["--from", "steps"])

    assert exit_info.value.code == EXIT_USAGE
    assert f"{CLARIFY_JSON}: его пишет --task N" in capsys.readouterr().err
    assert requests == []


def test_steps_without_an_assignment_on_disk_is_a_usage_error(
    llm: InstallResponses,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    a_review_of_an_earlier_run(tmp_path)
    requests = llm([])

    with pytest.raises(SystemExit) as exit_info:
        main(["--from", "steps"])

    assert exit_info.value.code == EXIT_USAGE
    assert "--task" in capsys.readouterr().err
    assert requests == []
