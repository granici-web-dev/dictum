import anthropic
import pytest

from app import stages
from app.config import settings
from app.stages import STAGES, MissingApiKey, StageError, load_prompt, run_stage
from tests.helpers import InstallResponses, ok, request_body, server_error

IDEA_BLOCK = (
    '<file path="inputs/idea.md">\n# Напоминания о дедлайнах\n\n'
    "## Суть\nБот присылает список.\n</file>"
)
BRIEF_BLOCK = (
    '<file path="outputs/brief.md">\n# Бриф: напоминания\n\n'
    "## 1. Пользователи\nКоманда из пяти человек.\n</file>"
)
INPUTS = {"inputs/transcript.md": "---\nsource: text\nlang: ru\n---\nХочу бота."}


def test_all_stage_prompts_exist() -> None:
    for s in STAGES:
        assert "description:" in load_prompt(s)


def test_anthropic_client_reports_a_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    stages.anthropic_client.cache_clear()

    with pytest.raises(MissingApiKey, match="ANTHROPIC_API_KEY is not set"):
        stages.anthropic_client()


def test_run_stage_parses_file_blocks_and_usage(llm: InstallResponses) -> None:
    two_files = (
        '<file path="outputs/issues.json">\n{"issues": []}\n</file>\n'
        '<file path="outputs/issues.md">\n# Issues\n</file>'
    )
    llm([ok(two_files)])

    result = run_stage("decompose", {"outputs/prd.md": "# PRD"})

    assert result.files == {
        "outputs/issues.json": '{"issues": []}\n',
        "outputs/issues.md": "# Issues\n",
    }
    assert result.model == "claude-sonnet-5"
    assert (result.input_tokens, result.output_tokens) == (120, 30)


def test_run_stage_sends_stage_prompt_and_inputs(llm: InstallResponses) -> None:
    requests = llm([ok(IDEA_BLOCK)])

    run_stage("intake", INPUTS)

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

    run_stage("brief", {"inputs/idea.md": "# Идея"}, params={"mode": "batch", "lang": "ru"})

    content = request_body(requests[0])["messages"][0]["content"]
    assert content.startswith("<params>\nmode: batch\nlang: ru\n</params>")


def test_run_stage_appends_user_edit(llm: InstallResponses) -> None:
    requests = llm([ok(BRIEF_BLOCK)])

    run_stage("brief", INPUTS, user_edit="Убери упоминание выходных")

    edit = "<user_edit>\nУбери упоминание выходных\n</user_edit>"
    assert request_body(requests[0])["messages"][0]["content"].endswith(edit)


def test_run_stage_prepends_history(llm: InstallResponses) -> None:
    requests = llm([ok(BRIEF_BLOCK)])
    history: list[anthropic.types.MessageParam] = [
        {"role": "user", "content": "Кто пользователи?"},
        {"role": "assistant", "content": "Команда из пяти человек."},
    ]

    run_stage("brief", INPUTS, history=history)

    messages = request_body(requests[0])["messages"]
    assert messages[:2] == history
    assert len(messages) == 3


def test_run_stage_uses_decompose_model(
    llm: InstallResponses, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "anthropic_model_decompose", "claude-opus-5")
    both = (
        '<file path="outputs/issues.json">\n{}\n</file>\n'
        '<file path="outputs/issues.md">\n# Issues\n</file>'
    )
    requests = llm([ok(both)])

    run_stage("decompose", {"outputs/prd.md": "# PRD"})

    assert request_body(requests[0])["model"] == "claude-opus-5"


def test_run_stage_retries_twice_on_server_error(llm: InstallResponses) -> None:
    requests = llm([server_error(), server_error(), ok(IDEA_BLOCK)])

    result = run_stage("intake", INPUTS)

    assert "inputs/idea.md" in result.files
    assert len(requests) == 3


def test_run_stage_raises_after_third_server_error(
    llm: InstallResponses, caplog: pytest.LogCaptureFixture
) -> None:
    requests = llm([server_error(), server_error(), server_error()])

    with pytest.raises(anthropic.InternalServerError):
        run_stage("intake", INPUTS)
    assert len(requests) == 3
    assert "stage=intake" in caplog.text
    assert "error=InternalServerError" in caplog.text


def test_run_stage_raises_when_output_truncated(llm: InstallResponses) -> None:
    truncated = '<file path="inputs/idea.md">\n# Обрыв'
    llm([ok(truncated, stop_reason="max_tokens")])

    with pytest.raises(StageError, match="Raise ANTHROPIC_MAX_TOKENS") as exc_info:
        run_stage("intake", INPUTS)
    assert exc_info.value.raw == truncated


def test_run_stage_raises_when_stage_returns_a_file_it_does_not_own(llm: InstallResponses) -> None:
    llm([ok('<file path="outputs/notes.md">\n# Заметки\n</file>')])

    with pytest.raises(StageError, match="expected inputs/idea.md or outputs/candidates.md"):
        run_stage("intake", INPUTS)


def test_run_stage_raises_when_decompose_returns_only_one_file(llm: InstallResponses) -> None:
    llm([ok('<file path="outputs/issues.json">\n{}\n</file>')])

    with pytest.raises(StageError, match="expected outputs/issues.json, outputs/issues.md"):
        run_stage("decompose", {"outputs/prd.md": "# PRD"})


def test_run_stage_raises_when_two_blocks_share_a_path(llm: InstallResponses) -> None:
    twice = (
        '<file path="inputs/idea.md">\n# Первая\n</file>\n'
        '<file path="inputs/idea.md">\n# Вторая\n</file>'
    )
    llm([ok(twice)])

    with pytest.raises(StageError, match="two <file> blocks share the path inputs/idea.md"):
        run_stage("intake", INPUTS)


def test_run_stage_raises_when_no_file_block(llm: InstallResponses) -> None:
    llm([ok("Вот идея: бот присылает список.")])

    with pytest.raises(StageError, match="got no <file> blocks") as exc_info:
        run_stage("intake", INPUTS)
    assert exc_info.value.raw == "Вот идея: бот присылает список."
