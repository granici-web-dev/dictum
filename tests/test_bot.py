import json
from datetime import timedelta
from pathlib import Path

import pytest
from telegram import Voice

from app import bot
from app.bot import (
    LABEL,
    MAX_VOICE_SECONDS,
    VOICE_INGEST_LABEL,
    allowed_chats,
    cards_published,
    demo_run,
    finished_text,
    main,
    progress_text,
    too_long,
)
from app.config import ConfigError, settings
from app.pipeline import NAMES


def a_voice(duration: int | timedelta) -> Voice:
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


def test_the_limit_holds_when_the_library_reports_a_timedelta() -> None:
    """Числом или timedelta — решает флаг совместимости PTB, а не мы."""
    assert too_long(a_voice(timedelta(seconds=MAX_VOICE_SECONDS + 1)))


def test_the_progress_calls_the_first_step_transcription_for_a_voice_run() -> None:
    printed = progress_text("прогон", [], "voice").splitlines()[2:]

    assert printed[0] == f"▸ {VOICE_INGEST_LABEL}"
    assert printed[1:] == [f"· {LABEL[name]}" for name in NAMES[1:]]


def test_the_demo_run_carries_the_recording_to_the_walk(tmp_path: Path) -> None:
    voice = tmp_path / "voice.oga"

    assert demo_run("прогон", audio=voice).audio == voice


def test_the_bot_does_not_start_without_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Проверка на старте, а не на первом голосовом: у стенда это уже поздно."""
    monkeypatch.setattr(settings, "telegram_bot_token", "token")
    monkeypatch.setattr(settings, "allow_live_api", True)
    listed(monkeypatch, "12")
    monkeypatch.setattr(bot, "ffmpeg_installed", lambda: False)

    with pytest.raises(ConfigError, match="ffmpeg"):
        main()

