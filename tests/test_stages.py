import json
import logging
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic import DefaultHttpxClient

from datetime import UTC, datetime

from app import stages
from app.answers import read_answers
from app.clarify import Clarify
from app.config import LiveApiNotAllowed, MissingApiKey, settings
from app.pipeline import STAGES
from app.render import issues_markdown, review_markdown, steps_markdown
from app.review import Review
from app.review import fragments
from app.project import project_snapshot, read_project
from app.steps import Steps
from app.stages import (
    StageError,
    load_prompt,
    run_stage,
)
from tests.helpers import (
    BROKEN_ISSUES,
    CLARIFY_DE,
    FIXTURES,
    REAL_ISSUES,
    STEPS_DE,
    InstallResponses,
    clarify_answer,
    decompose_answer,
    ok,
    real_issues,
    request_body,
    server_error,
    steps_answer,
)
from tests.test_review import MEETING_DE, REVIEW_DE, REVIEW_TICKET_DE, TICKET_DE

IDEA_BLOCK = (
    '<file path="inputs/idea.md">\n# Напоминания о дедлайнах\n\n'
    "## Summary\nБот присылает список.\n</file>"
)
BRIEF_BLOCK = (
    '<file path="outputs/brief.md">\n# Бриф: напоминания\n\n'
    "## 1. Users\nКоманда из пяти человек.\n</file>"
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
    assert f"stage=intake run={RUN}" in caplog.text
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


def candidates_answer(body: str) -> str:
    return f'<file path="outputs/candidates.md">\n{body}</file>'


WITHOUT_OUTCOME = "# В записи найдено 2 идеи\n\n1. **Раз** — одно.\n2. **Два** — другое.\n"
WITH_OUTCOME = "---\noutcome: multiple\n---\n\n" + WITHOUT_OUTCOME


def test_candidates_without_an_outcome_are_sent_back_for_repair(llm: InstallResponses) -> None:
    """Исход заявляет intake: по нему бот решает, показывать идеи или темы разговора."""
    requests = llm([ok(candidates_answer(WITHOUT_OUTCOME)), ok(candidates_answer(WITH_OUTCOME))])

    result = run_stage("intake", INPUTS, RUN)

    assert len(requests) == 2
    assert "во frontmatter нужно multiple или none" in request_body(requests[1])["messages"][-1][
        "content"
    ]
    assert "outcome: multiple" in result.files["outputs/candidates.md"]


def test_run_stage_gives_up_when_the_candidates_keep_their_shape_broken(
    llm: InstallResponses,
) -> None:
    broken = candidates_answer("---\noutcome: multiple\n---\n\n# Идей две\n\nПрозой.\n")
    requests = llm([ok(broken), ok(broken)])

    with pytest.raises(StageError, match="нет ни одной строки вида"):
        run_stage("intake", INPUTS, RUN)

    assert len(requests) == 2


def test_run_stage_gives_up_after_one_repair_attempt(llm: InstallResponses) -> None:
    requests = llm([ok("Без тегов."), ok("Снова без тегов.")])

    with pytest.raises(StageError, match="got no <file> blocks") as exc_info:
        run_stage("intake", INPUTS, RUN)

    assert len(requests) == 2
    assert exc_info.value.raw == "Снова без тегов."


def test_every_call_of_the_model_names_the_run_it_was_paid_for(
    llm: InstallResponses, caplog: pytest.LogCaptureFixture
) -> None:
    """CONVENTIONS требует run_id в каждой записи вызова: без него строки стадий ничьи."""
    llm([ok(IDEA_BLOCK)])

    with caplog.at_level(logging.INFO, logger="app.stages"):
        run_stage("intake", INPUTS, RUN)

    assert f"stage=intake run={RUN} model=" in caplog.text


def test_an_extra_file_beside_the_whole_expected_set_is_dropped_with_a_line(
    llm: InstallResponses, caplog: pytest.LogCaptureFixture
) -> None:
    """Живой прогон 2ebcf8868ca3dbf9: decompose прислал ещё и issues.md, и прогон умер.

    Копию всё равно рисует код из того же JSON, и ремонтного повтора она не стоит.
    """
    llm([ok(decompose_answer() + '\n<file path="outputs/issues.md">\n# Чужой бэклог\n</file>')])

    with caplog.at_level(logging.WARNING, logger="app.stages"):
        result = run_stage("decompose", {"outputs/prd.md": "# PRD"}, RUN)

    assert "stage=decompose extra=outputs/issues.md" in caplog.text
    assert result.files["outputs/issues.md"] == issues_markdown(real_issues())


def test_a_missing_file_is_still_an_error_and_not_a_dropped_extra(llm: InstallResponses) -> None:
    """Выбрасывается лишнее при полном наборе; неполный набор остаётся ошибкой."""
    llm([ok('<file path="outputs/issues.md">\n# Только проза\n</file>')] * 2)

    with pytest.raises(StageError, match="expected outputs/issues.json"):
        run_stage("decompose", {"outputs/prd.md": "# PRD"}, RUN)


REVIEW_INPUTS = {"inputs/transcript.md": MEETING_DE}
OWNER_RU = {"owner_lang": "ru"}
# В фикстуре одна цитата расходится с расшифровкой («Fehlermeldungen» против сказанного
# «Fehlermeldung»): так выглядит причёсанный моделью текст распознавания.
INVENTED_QUOTE = "Die Fehlermeldungen auf der Kontoseite sind noch auf Englisch."


def review_answer(text: str = REVIEW_DE, model_says_found: bool = True) -> str:
    """Ответ модели: разбор из фикстуры, где поля кода стоят так, как их прислала бы модель."""
    data = json.loads(text)
    del data["owner_lang"]
    for task in data["tasks"]:
        for fragment in [task["deadline"], *task["constraints"], *task["do_not"], *task["quotes"]]:
            if fragment:
                fragment["in_transcript"] = model_says_found
    return f'<file path="outputs/review.json">\n{json.dumps(data, ensure_ascii=False)}\n</file>'


def said_verbatim() -> str:
    said = INVENTED_QUOTE.replace("Fehlermeldungen", "Fehlermeldung")
    return review_answer(REVIEW_DE.replace(INVENTED_QUOTE, said))


def found_marks(result_json: str) -> dict[str, bool | None]:
    review = Review.model_validate_json(result_json)
    return {fragment.original: fragment.in_transcript for fragment in fragments(review)}


def test_a_review_said_verbatim_is_stamped_and_drawn_without_a_repair(
    llm: InstallResponses,
) -> None:
    requests = llm([ok(said_verbatim())])

    result = run_stage("review", REVIEW_INPUTS, RUN, params=OWNER_RU)

    assert len(requests) == 1
    written = json.loads(result.files["outputs/review.json"])
    assert written["owner_lang"] == "ru"
    assert set(found_marks(result.files["outputs/review.json"]).values()) == {True}
    assert result.files["outputs/review.md"] == review_markdown(
        Review.model_validate_json(result.files["outputs/review.json"])
    )


def test_a_quote_nobody_said_gets_the_one_repair_and_is_named_in_it(
    llm: InstallResponses,
) -> None:
    requests = llm([ok(review_answer()), ok(said_verbatim())])

    result = run_stage("review", REVIEW_INPUTS, RUN, params=OWNER_RU)

    assert len(requests) == 2
    assert INVENTED_QUOTE in request_body(requests[1])["messages"][-1]["content"]
    assert set(found_marks(result.files["outputs/review.json"]).values()) == {True}


def test_review_marks_a_fragment_still_missing_after_the_repair_instead_of_failing(
    llm: InstallResponses, caplog: pytest.LogCaptureFixture
) -> None:
    """Правило держит этот тест: пометка вместо отказа, и `true` модели ничего не стоит."""
    requests = llm([ok(review_answer()), ok(review_answer())])

    with caplog.at_level(logging.INFO, logger="app.stages"):
        result = run_stage("review", REVIEW_INPUTS, RUN, params=OWNER_RU)

    assert len(requests) == 2
    marks = found_marks(result.files["outputs/review.json"])
    assert marks.pop(INVENTED_QUOTE) is False
    assert set(marks.values()) == {True}
    assert f"stage=review run={RUN} unverified=1" in caplog.text


def test_a_repair_that_breaks_the_json_still_fails_the_review(llm: InstallResponses) -> None:
    llm([ok(review_answer()), ok('<file path="outputs/review.json">\n{"tasks": [\n</file>')])

    with pytest.raises(StageError, match="не разбирается как JSON"):
        run_stage("review", REVIEW_INPUTS, RUN, params=OWNER_RU)


def test_an_empty_translation_of_a_german_recording_fails_after_the_repair(
    llm: InstallResponses,
) -> None:
    untranslated = said_verbatim().replace(
        '"translation": "Во-первых, форма входа."', '"translation": ""'
    )
    requests = llm([ok(untranslated), ok(untranslated)])

    with pytest.raises(StageError, match=r"tasks\.0\.quotes\.0\.translation: перевод пуст"):
        run_stage("review", REVIEW_INPUTS, RUN, params=OWNER_RU)

    assert len(requests) == 2


def ticket_answer(ticket_key: str) -> str:
    data = json.loads(REVIEW_TICKET_DE)
    del data["owner_lang"]
    data["tasks"][0]["ticket_key"] = ticket_key
    return f'<file path="outputs/review.json">\n{json.dumps(data, ensure_ascii=False)}\n</file>'


def test_an_invented_ticket_key_gets_the_repair_and_is_dropped_if_it_stays(
    llm: InstallResponses,
) -> None:
    requests = llm([ok(ticket_answer("ABC-124")), ok(ticket_answer("ABC-124"))])

    result = run_stage("review", {"inputs/transcript.md": TICKET_DE}, RUN, params=OWNER_RU)

    assert len(requests) == 2
    assert "ABC-124" in request_body(requests[1])["messages"][-1]["content"]
    [task] = Review.model_validate_json(result.files["outputs/review.json"]).tasks
    assert (task.ticket_key, task.ticket_url) == (None, "https://jira.example.com/browse/ABC-123")


UNSET_PROJECT = project_snapshot(None, datetime(2026, 9, 17, 10, 2, tzinfo=UTC))
PARTIAL_ANSWERS = (FIXTURES / "answers_de_partial.md").read_text(encoding="utf-8")
CLARIFY_INPUTS = {
    "inputs/assignment.json": (FIXTURES / "assignment_de.json").read_text(encoding="utf-8"),
    "inputs/project.md": UNSET_PROJECT,
}
ASSIGNMENT_INPUTS = {
    "inputs/assignment.json": CLARIFY_INPUTS["inputs/assignment.json"],
    "outputs/clarify.json": CLARIFY_DE,
    "inputs/answers.md": PARTIAL_ANSWERS,
    "inputs/project.md": UNSET_PROJECT,
}


def untranslated_step(data: dict[str, Any]) -> None:
    data["steps"][1]["translation"] = ""


def test_steps_come_back_stamped_and_drawn_without_a_repair(llm: InstallResponses) -> None:
    requests = llm([ok(steps_answer())])

    result = run_stage("steps", ASSIGNMENT_INPUTS, RUN)

    assert len(requests) == 1
    assert result.files["outputs/steps.json"] == STEPS_DE
    assert result.files["outputs/steps.md"] == steps_markdown(
        Steps.model_validate_json(STEPS_DE),
        read_answers(PARTIAL_ANSWERS),
        read_project(UNSET_PROJECT),
    )


def test_an_empty_step_translation_gets_the_one_repair_and_is_named_in_it(
    llm: InstallResponses,
) -> None:
    requests = llm([ok(steps_answer(untranslated_step)), ok(steps_answer())])

    result = run_stage("steps", ASSIGNMENT_INPUTS, RUN)

    assert len(requests) == 2
    repair = request_body(requests[1])["messages"][-1]["content"]
    assert "steps.1.translation: перевод пуст" in repair
    assert result.files["outputs/steps.json"] == STEPS_DE


def test_an_empty_step_translation_in_both_answers_fails_the_stage(
    llm: InstallResponses,
) -> None:
    requests = llm([ok(steps_answer(untranslated_step)), ok(steps_answer(untranslated_step))])

    with pytest.raises(StageError, match=r"steps\.1\.translation: перевод пуст"):
        run_stage("steps", ASSIGNMENT_INPUTS, RUN)

    assert len(requests) == 2


def untranslated_question(data: dict[str, Any]) -> None:
    data["questions"][0]["translation"] = ""


def test_questions_come_back_stamped_without_a_repair_and_see_the_project(
    llm: InstallResponses,
) -> None:
    requests = llm([ok(clarify_answer())])

    result = run_stage("clarify", CLARIFY_INPUTS, RUN)

    assert len(requests) == 1
    assert set(result.files) == {"outputs/clarify.json"}
    stamped = Clarify.model_validate_json(result.files["outputs/clarify.json"])
    assert stamped == Clarify.model_validate_json(CLARIFY_DE)
    assert '<file path="inputs/project.md">' in request_body(requests[0])["messages"][0]["content"]


def test_an_empty_question_translation_gets_the_one_repair_and_is_named_in_it(
    llm: InstallResponses,
) -> None:
    requests = llm([ok(clarify_answer(untranslated_question)), ok(clarify_answer())])

    result = run_stage("clarify", CLARIFY_INPUTS, RUN)

    assert len(requests) == 2
    repair = request_body(requests[1])["messages"][-1]["content"]
    assert "questions.0.translation: перевод пуст" in repair
    assert result.files["outputs/clarify.json"] == CLARIFY_DE


@pytest.mark.parametrize("stage", ["clarify", "steps"])
def test_a_task_stage_does_not_pay_for_a_call_on_an_assignment_that_is_not_one(
    llm: InstallResponses, stage: str
) -> None:
    requests = llm([])
    broken = json.loads(ASSIGNMENT_INPUTS["inputs/assignment.json"])
    del broken["owner_lang"]
    inputs = {**ASSIGNMENT_INPUTS, "inputs/assignment.json": json.dumps(broken)}

    with pytest.raises(StageError, match="assignment.json не проходит схему"):
        run_stage(stage, inputs, RUN)

    assert requests == []


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("outputs/clarify.json", "{}"),
        ("inputs/answers.md", "---\nstatus: answered\n---\n"),
    ],
)
def test_steps_do_not_pay_for_a_call_on_questions_or_answers_that_are_not_them(
    llm: InstallResponses, path: str, content: str
) -> None:
    """Ответ без времени и текста правили руками: какие вопросы он закрыл, судить не по чему."""
    requests = llm([])

    with pytest.raises(StageError, match=f"{path} не проходит схему"):
        run_stage("steps", {**ASSIGNMENT_INPUTS, path: content}, RUN)

    assert requests == []


def test_an_unanswered_number_outside_the_questions_gets_the_one_repair(
    llm: InstallResponses,
) -> None:
    def ninth(data: dict[str, Any]) -> None:
        data["unanswered"] = [9]

    requests = llm([ok(steps_answer(ninth)), ok(steps_answer())])

    result = run_stage("steps", ASSIGNMENT_INPUTS, RUN)

    assert "unanswered: вопроса 9 нет" in request_body(requests[1])["messages"][-1]["content"]
    assert result.files["outputs/steps.json"] == STEPS_DE
