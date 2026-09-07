from pathlib import Path

import pytest

from app.cli import EXIT_NEEDS_A_DECISION, EXIT_OK, EXIT_STAGE_FAILED, main
from tests.conftest import InstallResponses, ok, request_body

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

    written = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*.*"))
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
    assert brief_message.startswith(
        "<params>\nmode: batch\ninteractive: false\nlang: ru\n</params>"
    )


def test_run_text_feeds_prd_the_brief_research_and_template(
    llm: InstallResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    requests = llm([ok(IDEA_BLOCK), ok(BRIEF_BLOCK), ok(PRD_BLOCK), ok(ISSUES_BLOCKS)])

    main([TEXT, "--lang", "ru"])

    prd_message = request_body(requests[2])["messages"][0]["content"]
    assert '<file path="outputs/brief.md">' in prd_message
    assert "Ресёрч пропущен по решению пользователя" in prd_message
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
    assert not (tmp_path / "outputs/issues.md").exists()
