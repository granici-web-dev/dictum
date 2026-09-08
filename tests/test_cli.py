import json
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
from app.pipeline import BRIEF, CANDIDATES, PRD, RESEARCH, TRANSCRIPT
from tests.helpers import (
    BROKEN_ISSUES,
    InstallResponses,
    decompose_answer,
    ok,
    request_body,
    server_error,
)

EARLIER_RUN = "прогон-восемь"


def transcript_of_an_earlier_run(root: Path) -> Path:
    path = root / TRANSCRIPT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_transcript("Идея с прошлого прогона.", "ru", EARLIER_RUN), "utf-8")
    return path


IDEA_BLOCK = (
    '<file path="inputs/idea.md">\n---\nsource: text\nlang: ru\nconfidence: high\n---\n\n'
    "# Напоминания о дедлайнах\n\n## Суть\nБот присылает список карточек.\n</file>"
)
CANDIDATES_BLOCK = (
    '<file path="outputs/candidates.md">\n# В записи найдено 2 идеи\n\n'
    "1. **Напоминания** — бот шлёт список.\n2. **Отчёты** — сводка за неделю.\n</file>"
)
BRIEF_BLOCK = (
    '<file path="outputs/brief.md">\n---\nname: Напоминания\nkind: product\nlang: ru\n'
    "open_questions: 2\n---\n\n# Бриф\n\n## 1. Пользователи\n[уточнить: размер команды]\n</file>"
)
PRD_BLOCK = (
    '<file path="outputs/prd.md">\n# PRD: Напоминания\n\n## Скоп MVP\n'
    "| ID | Сценарий | Приоритет | Зависит от | Критерий готовности |\n"
    "|---|---|---|---|---|\n| S1 | Утренний список | Must | — | Список пришёл в 9:00 |\n</file>"
)
ISSUES_BLOCKS = decompose_answer()
TEXT = "Хочу, чтобы бот напоминал о дедлайнах в Trello"
PRD_FROM_A_REAL_RUN = (Path(__file__).parent.parent / "fixtures/prd_real.md").read_text(
    encoding="utf-8"
)


def test_run_text_writes_the_artifact_of_every_stage(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])

    assert main([TEXT, "--lang", "ru"]) == EXIT_OK

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


def test_run_text_writes_the_transcript_frontmatter(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])

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
    llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])

    assert main(["--file", str(idea), "--lang", "ru"]) == EXIT_OK
    assert TEXT in (tmp_path / "inputs/transcript.md").read_text(encoding="utf-8")


def test_run_text_tells_brief_that_nobody_will_answer(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    requests = llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])

    main([TEXT, "--lang", "ru"])

    brief_message = request_body(requests[1])["messages"][0]["content"]
    # Только блок <params>: «lang: ru» стоит ещё и во frontmatter самого idea.md, который едет
    # тем же сообщением, и проверка по всему тексту проходила бы с пустыми параметрами.
    params = brief_message.split("<params>\n", 1)[1].split("\n</params>", 1)[0]
    assert params.splitlines() == ["mode: batch", "interactive: false", "lang: ru"]


def test_run_text_feeds_prd_the_brief_research_and_template(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    requests = llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])

    main([TEXT, "--lang", "ru"])

    prd_message = request_body(requests[2])["messages"][0]["content"]
    assert '<file path="outputs/brief.md">' in prd_message
    assert "Ресёрч не запускался" in prd_message
    assert "Скоп MVP" in prd_message


def test_run_text_stops_when_intake_returns_candidates(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    requests = llm([ok(CANDIDATES_BLOCK)])

    assert main([TEXT, "--lang", "ru"]) == EXIT_NEEDS_A_DECISION

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
    requests = llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK)])

    with caplog.at_level(logging.INFO, logger="app.cli"):
        assert main([TEXT, "--lang", "ru", "--gates"]) == EXIT_NEEDS_A_DECISION

    assert len(requests) == 2
    assert (tmp_path / BRIEF).is_file()
    assert not (tmp_path / PRD).exists()
    assert "--from research" in caplog.text


def test_run_text_saves_the_raw_answer_of_a_failed_stage(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    empty = '<file path="outputs/issues.json">\n{"issues": []}\n</file>'
    llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(empty), ok(empty)])

    assert main([TEXT, "--lang", "ru"]) == EXIT_STAGE_FAILED

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


def test_run_text_takes_the_language_from_the_input_file(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    idea = tmp_path / "idea.md"
    idea.write_text(f"---\nsource: text\nlang: ru\n---\n\n{TEXT}\n", encoding="utf-8")
    llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])

    assert main(["--file", str(idea)]) == EXIT_OK

    transcript = (tmp_path / "inputs/transcript.md").read_text(encoding="utf-8")
    assert transcript.count("---") == 2
    assert "lang: ru" in transcript
    assert "lang: de" not in transcript


def test_run_text_falls_back_to_the_configured_language(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])

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
    monkeypatch.chdir(tmp_path)
    llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])

    assert main([TEXT, "--lang", "ru"]) == EXIT_OK

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
