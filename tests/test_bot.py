import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from telegram import Message, Voice

from app import bot
from app.bot import (
    BUSY,
    LABEL,
    MAX_VOICE_SECONDS,
    NO_SINGLE_IDEA,
    VOICE_INGEST_LABEL,
    allowed_chats,
    cards_published,
    demo_run,
    finished_text,
    follow,
    main,
    outcome,
    progress_text,
    refuse,
    too_long,
)
from app.config import ConfigError, settings
from app.pipeline import CANDIDATES, NAMES, Stage, stages_between
from app.run import Pause, Run


def a_voice(duration: int) -> Voice:
    return Voice(file_id="f", file_unique_id="u", duration=duration)


def listed(monkeypatch: pytest.MonkeyPatch, written: str) -> None:
    monkeypatch.setattr(settings, "telegram_allowed_chat_ids", written)


def test_an_empty_allowlist_stops_the_bot_instead_of_letting_everyone_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listed(monkeypatch, "")

    with pytest.raises(ConfigError, match="TELEGRAM_ALLOWED_CHAT_IDS пуст"):
        allowed_chats()


def test_a_list_of_only_separators_counts_as_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    listed(monkeypatch, " , , ")

    with pytest.raises(ConfigError, match="пуст"):
        allowed_chats()


def test_group_and_personal_ids_survive_the_spaces_around_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listed(monkeypatch, " 12 ,-100500,  7 ")

    assert allowed_chats() == frozenset({12, -100500, 7})


def test_something_that_is_not_an_id_is_named_in_the_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listed(monkeypatch, "12, @сергей, 13")

    with pytest.raises(ConfigError, match="@сергей"):
        allowed_chats()


def test_the_progress_shows_every_stage_of_the_walk_in_its_order() -> None:
    text = progress_text("прогон", [], "text")

    printed = [line[2:] for line in text.splitlines()[2:]]
    assert printed == [LABEL[name] for name in NAMES]


def test_the_progress_marks_what_is_done_and_what_runs_now() -> None:
    text = progress_text("прогон", ["ingest", "intake"], "text")

    marks = [line[0] for line in text.splitlines()[2:]]
    assert marks == ["✓", "✓", "▸", "·", "·", "·", "·"]


def test_a_finished_walk_marks_everything_and_points_at_nothing() -> None:
    text = progress_text("прогон", list(NAMES), "text")

    assert [line[0] for line in text.splitlines()[2:]] == ["✓"] * len(NAMES)


def test_the_progress_carries_the_run_id_so_the_log_can_be_found() -> None:
    assert progress_text("a1b2c3d4", [], "text").startswith("Прогон a1b2c3d4")


def test_the_last_message_counts_the_cards_and_links_the_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "trello_board_id", "JcGpvPdx")
    journal = tmp_path / "outputs/publish.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({f"I-{n:03}": {} for n in range(1, 16)}), encoding="utf-8")

    text = finished_text(tmp_path)

    assert "15 карточек" in text
    assert "https://trello.com/b/JcGpvPdx" in text


def test_the_card_count_comes_from_the_journal_of_that_run(tmp_path: Path) -> None:
    journal = tmp_path / "outputs/publish.json"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"I-001": {}, "S7": {}}', encoding="utf-8")

    assert cards_published(tmp_path) == 2


def test_every_stage_of_the_walk_has_something_to_show_a_person() -> None:
    assert set(LABEL) == set(NAMES)


def test_the_demo_run_is_auto_approved_by_the_flag_it_sets_itself() -> None:
    """Ворота бот проходит режимом прогона, а не тем, что кнопок для них ещё нет."""
    assert demo_run("прогон", text="Идея").auto_approve


def test_a_voice_at_the_limit_runs_and_a_second_over_it_does_not() -> None:
    assert not too_long(a_voice(MAX_VOICE_SECONDS))
    assert too_long(a_voice(MAX_VOICE_SECONDS + 1))


def test_the_progress_calls_the_first_step_transcription_for_a_voice_run() -> None:
    printed = progress_text("прогон", [], "voice").splitlines()[2:]

    assert printed[0] == f"▸ {VOICE_INGEST_LABEL}"
    assert printed[1:] == [f"· {LABEL[name]}" for name in NAMES[1:]]


def test_the_bot_does_not_start_without_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Проверка на старте, а не на первом голосовом: у стенда это уже поздно."""
    monkeypatch.setattr(settings, "telegram_bot_token", "token")
    monkeypatch.setattr(settings, "allow_live_api", True)
    listed(monkeypatch, "12")
    monkeypatch.setattr(bot, "ffmpeg_installed", lambda: False)

    with pytest.raises(ConfigError, match="ffmpeg"):
        main()


class SlowNote:
    """Сообщение, у которого правка прогресса отвечает медленнее финальной.

    Ровно так прогон 0ac7bdffe0e0ba58 и потерял ссылку: ответ на последнюю правку прогресса
    пришёл после ответа на правку со ссылкой и затёр её списком галочек. При одинаковых
    задержках порядок сохраняется сам собой, и тест проходит даже на сломанном коде.
    """

    def __init__(self) -> None:
        self.edits: list[str] = []

    async def edit_text(self, text: str) -> Message:
        await asyncio.sleep(0.05 if text.startswith("Прогон") else 0)
        self.edits.append(text)
        return cast(Message, self)


def walk_reporting_every_stage(
    run: Run, start: str, stop: str, on_done: Callable[[Stage], None]
) -> Pause | None:
    for stage in stages_between(start, stop):
        on_done(stage)
    return None


@pytest.mark.asyncio
async def test_the_link_is_the_last_thing_the_message_shows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Правки прогресса не ждут ответа Telegram и однажды затёрли ссылку списком галочек."""
    journal = tmp_path / "outputs/publish.json"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"I-001": {}}', encoding="utf-8")
    monkeypatch.setattr(settings, "trello_board_id", "board1")
    monkeypatch.setattr(bot, "walk", walk_reporting_every_stage)
    note = SlowNote()

    await follow(
        cast(Message, note),
        Run(root=tmp_path, run_id="прогон", lang="ru", text="Идея", auto_approve=True),
    )

    assert note.edits[-1] == finished_text(tmp_path)
    assert len(note.edits) == len(NAMES) + 1


@pytest.mark.asyncio
async def test_the_refusal_after_intake_does_not_count_ideas_it_did_not_find(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Кандидаты бывают и от пустоты: на двухсекундном голосовом idea не нашлась вовсе."""

    def walk_stopping_on_choice(
        run: Run, start: str, stop: str, on_done: Callable[[Stage], None]
    ) -> Pause | None:
        return Pause(stage="intake", artifact=CANDIDATES, kind="choice")

    monkeypatch.setattr(bot, "walk", walk_stopping_on_choice)

    said = await outcome(
        Run(root=tmp_path, run_id="прогон", lang="ru", text="…", auto_approve=True),
        lambda stage: None,
    )

    assert said == NO_SINGLE_IDEA


class QuietChat:
    """Сообщение из чата 12: отвечать умеет, больше от него ничего не нужно."""

    chat_id = 12

    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> Message:
        self.replies.append(text)
        return cast(Message, self)


@pytest.mark.asyncio
async def test_a_refusal_leaves_a_line_the_rehearsal_can_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Отчёт P2-06 считает отказы grep-ом, поэтому у каждого свой tag и id чата рядом."""
    chat = QuietChat()

    with caplog.at_level("INFO", logger="app.bot"):
        await refuse(cast(Message, chat), "busy", BUSY)

    assert "refusal=busy chat=12" in caplog.text
    assert chat.replies == [BUSY]
