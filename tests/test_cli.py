from pathlib import Path

import pytest

from app.cli import (
    BRIEF,
    CANDIDATES,
    EXIT_NEEDS_A_DECISION,
    EXIT_OK,
    EXIT_STAGE_FAILED,
    EXIT_USAGE,
    IDEA,
    PRD,
    main,
)
from app.stages import STAGE_OUTPUTS
from app import stages
from app.config import settings
from tests.conftest import InstallResponses, ok, request_body, server_error

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
ISSUES_BLOCKS = (
    '<file path="outputs/issues.json">\n{"issues": []}\n</file>\n'
    '<file path="outputs/issues.md">\n# Issues\n\n## Фаза 1\n</file>'
)
TEXT = "Хочу, чтобы бот напоминал о дедлайнах в Trello"


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

    transcript = (tmp_path / "inputs/transcript.md").read_text(encoding="utf-8")
    assert transcript.startswith("---\nsource: text\nduration: null\nlang: ru\n---\n")
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
    assert "mode: batch" in brief_message
    assert "interactive: false" in brief_message
    assert "lang: ru" in brief_message


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
    assert (tmp_path / "outputs/candidates.md").exists()
    assert not (tmp_path / "outputs/brief.md").exists()


def test_run_text_saves_the_raw_answer_of_a_failed_stage(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    half = '<file path="outputs/issues.json">\n{"issues": []}\n</file>'
    llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(half)])

    assert main([TEXT, "--lang", "ru"]) == EXIT_STAGE_FAILED

    assert (tmp_path / "outputs/decompose.raw.md").read_text(encoding="utf-8") == half
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


def test_the_paths_the_cli_expects_are_the_ones_the_stages_may_return() -> None:
    assert {IDEA, CANDIDATES} == set().union(*STAGE_OUTPUTS["intake"])
    assert {BRIEF} == set().union(*STAGE_OUTPUTS["brief"])
    assert {PRD} == set().union(*STAGE_OUTPUTS["prd"])
