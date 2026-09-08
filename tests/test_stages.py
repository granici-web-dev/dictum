import json
import logging

import anthropic
import httpx2
import pytest
from anthropic import DefaultHttpxClient

from app import stages
from app.config import LiveApiNotAllowed, MissingApiKey, settings
from app.pipeline import STAGES
from app.render import issues_markdown
from app.stages import (
    StageError,
    load_prompt,
    run_stage,
)
from tests.helpers import (
    BROKEN_ISSUES,
    REAL_ISSUES,
    InstallResponses,
    decompose_answer,
    ok,
    real_issues,
    request_body,
    server_error,
)

IDEA_BLOCK = (
    '<file path="inputs/idea.md">\n# Напоминания о дедлайнах\n\n'
    "## Суть\nБот присылает список.\n</file>"
)
BRIEF_BLOCK = (
    '<file path="outputs/brief.md">\n# Бриф: напоминания\n\n'
    "## 1. Пользователи\nКоманда из пяти человек.\n</file>"
)
RUN = "прогон-для-теста"

INPUTS = {"inputs/transcript.md": "---\nsource: text\nlang: ru\n---\nХочу бота."}


def test_all_stage_prompts_exist() -> None:
    for stage in STAGES:
        if stage.runs == "llm":
            assert "description:" in load_prompt(stage.name)


def test_anthropic_client_reports_a_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "allow_live_api", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    stages.anthropic_client.cache_clear()

    with pytest.raises(MissingApiKey, match="ANTHROPIC_API_KEY is not set"):
        stages.anthropic_client()


def test_no_request_leaves_the_process_without_the_live_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "allow_live_api", False)
    monkeypatch.setattr(settings, "anthropic_api_key", "test")
    sent: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(200, json={})

    monkeypatch.setattr(
        stages,
        "http_client",
        lambda: DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    stages.anthropic_client.cache_clear()

    with pytest.raises(LiveApiNotAllowed, match="ALLOW_LIVE_API is not true"):
        run_stage("intake", INPUTS, RUN)
    assert sent == []


def test_run_stage_parses_file_blocks_and_usage(llm: InstallResponses) -> None:
    llm([ok(decompose_answer())])

    result = run_stage("decompose", {"outputs/prd.md": "# PRD"}, RUN)

    assert set(result.files) == {"outputs/issues.json", "outputs/issues.md"}
    assert json.loads(result.files["outputs/issues.json"]) == json.loads(REAL_ISSUES) | {
        "run_id": RUN
    }
    assert result.files["outputs/issues.md"] == issues_markdown(real_issues())
    assert result.model == "claude-sonnet-5"
    assert (result.input_tokens, result.output_tokens) == (120, 30)


def test_run_stage_sends_stage_prompt_and_inputs(llm: InstallResponses) -> None:
    requests = llm([ok(IDEA_BLOCK)])

    run_stage("intake", INPUTS, RUN)

    body = request_body(requests[0])
    assert body["model"] == settings.anthropic_model
    assert body["max_tokens"] == settings.anthropic_max_tokens
    assert body["system"].startswith(load_prompt("intake"))
    assert "## Режим API" in body["system"]
    transcript = INPUTS["inputs/transcript.md"]
    expected = f'<file path="inputs/transcript.md">\n{transcript}\n</file>'
    assert body["messages"] == [{"role": "user", "content": expected}]


def test_run_stage_sends_params_block(llm: InstallResponses) -> None:
    requests = llm([ok(BRIEF_BLOCK)])

    run_stage("brief", {"inputs/idea.md": "# Идея"}, RUN, params={"mode": "batch", "lang": "ru"})

    content = request_body(requests[0])["messages"][0]["content"]
    assert content.startswith("<params>\nmode: batch\nlang: ru\n</params>")


def test_run_stage_appends_user_edit(llm: InstallResponses) -> None:
    requests = llm([ok(BRIEF_BLOCK)])

    run_stage("brief", INPUTS, RUN, user_edit="Убери упоминание выходных")

    edit = "<user_edit>\nУбери упоминание выходных\n</user_edit>"
    assert request_body(requests[0])["messages"][0]["content"].endswith(edit)


def test_run_stage_prepends_history(llm: InstallResponses) -> None:
    requests = llm([ok(BRIEF_BLOCK)])
    history: list[anthropic.types.MessageParam] = [
        {"role": "user", "content": "Кто пользователи?"},
        {"role": "assistant", "content": "Команда из пяти человек."},
    ]

    run_stage("brief", INPUTS, RUN, history=history)

    messages = request_body(requests[0])["messages"]
    assert messages[:2] == history
    assert len(messages) == 3


def test_run_stage_uses_decompose_model(
    llm: InstallResponses, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "anthropic_model_decompose", "claude-opus-5")
    requests = llm([ok(decompose_answer())])

    run_stage("decompose", {"outputs/prd.md": "# PRD"}, RUN)

    assert request_body(requests[0])["model"] == "claude-opus-5"


def test_run_stage_retries_twice_on_server_error(llm: InstallResponses) -> None:
    requests = llm([server_error(), server_error(), ok(IDEA_BLOCK)])

    result = run_stage("intake", INPUTS, RUN)

    assert "inputs/idea.md" in result.files
    assert len(requests) == 3


def test_run_stage_raises_after_third_server_error(
    llm: InstallResponses, caplog: pytest.LogCaptureFixture
) -> None:
    requests = llm([server_error(), server_error(), server_error()])

    with pytest.raises(anthropic.InternalServerError):
        run_stage("intake", INPUTS, RUN)
    assert len(requests) == 3
    assert "stage=intake" in caplog.text
    assert "error=InternalServerError" in caplog.text


def test_run_stage_raises_when_output_truncated(llm: InstallResponses) -> None:
    truncated = '<file path="inputs/idea.md">\n# Обрыв'
    llm([ok(truncated, stop_reason="max_tokens")])

    with pytest.raises(StageError, match="Raise ANTHROPIC_MAX_TOKENS") as exc_info:
        run_stage("intake", INPUTS, RUN)
    assert exc_info.value.raw == truncated


def test_run_stage_raises_when_stage_returns_a_file_it_does_not_own(llm: InstallResponses) -> None:
    llm([ok('<file path="outputs/notes.md">\n# Заметки\n</file>')])

    with pytest.raises(StageError, match="expected inputs/idea.md or outputs/candidates.md"):
        run_stage("intake", INPUTS, RUN)


def test_run_stage_raises_when_decompose_writes_the_page_itself(llm: InstallResponses) -> None:
    both = decompose_answer() + '\n<file path="outputs/issues.md">\n# Бэклог\n</file>'
    llm([ok(both)])

    with pytest.raises(StageError, match="expected outputs/issues.json"):
        run_stage("decompose", {"outputs/prd.md": "# PRD"}, RUN)


def test_run_stage_raises_when_two_blocks_share_a_path(llm: InstallResponses) -> None:
    twice = (
        '<file path="inputs/idea.md">\n# Первая\n</file>\n'
        '<file path="inputs/idea.md">\n# Вторая\n</file>'
    )
    llm([ok(twice)])

    with pytest.raises(StageError, match="two <file> blocks share the path inputs/idea.md"):
        run_stage("intake", INPUTS, RUN)


def test_run_stage_asks_again_when_the_answer_has_no_file_blocks(llm: InstallResponses) -> None:
    requests = llm([ok("Вот идея, но я забыл теги."), ok(IDEA_BLOCK)])

    result = run_stage("intake", INPUTS, RUN)

    assert "inputs/idea.md" in result.files
    assert len(requests) == 2
    repair = request_body(requests[1])["messages"]
    assert repair[-2] == {"role": "assistant", "content": "Вот идея, но я забыл теги."}
    assert 'в ответе нет ни одного тега <file path="...">' in repair[-1]["content"]
    assert (result.input_tokens, result.output_tokens) == (240, 60)


def test_run_stage_asks_decompose_again_when_the_issues_do_not_validate(
    llm: InstallResponses,
) -> None:
    requests = llm([ok(decompose_answer(BROKEN_ISSUES)), ok(decompose_answer())])

    result = run_stage("decompose", {"outputs/prd.md": "# PRD"}, RUN)

    assert set(result.files) == {"outputs/issues.json", "outputs/issues.md"}
    assert len(requests) == 2
    complaint = request_body(requests[1])["messages"][-1]["content"]
    assert "deferred.0.title:" in complaint
    assert "Меняй только то, на что указано" in complaint


def test_a_repair_that_worked_still_says_what_was_wrong(
    llm: InstallResponses, caplog: pytest.LogCaptureFixture
) -> None:
    llm([ok(decompose_answer(BROKEN_ISSUES)), ok(decompose_answer())])

    with caplog.at_level(logging.WARNING, logger="app.stages"):
        run_stage("decompose", {"outputs/prd.md": "# PRD"}, RUN)

    assert "deferred.0.title: Field required" in caplog.text


def test_run_stage_gives_up_when_the_issues_are_still_invalid(llm: InstallResponses) -> None:
    requests = llm([ok(decompose_answer(BROKEN_ISSUES)), ok(decompose_answer(BROKEN_ISSUES))])

    with pytest.raises(StageError, match=r"deferred\.0\.title:"):
        run_stage("decompose", {"outputs/prd.md": "# PRD"}, RUN)

    assert len(requests) == 2


def test_run_stage_gives_up_after_one_repair_attempt(llm: InstallResponses) -> None:
    requests = llm([ok("Без тегов."), ok("Снова без тегов.")])

    with pytest.raises(StageError, match="got no <file> blocks") as exc_info:
        run_stage("intake", INPUTS, RUN)

    assert len(requests) == 2
    assert exc_info.value.raw == "Снова без тегов."
