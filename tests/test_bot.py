import asyncio
import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.exc import OperationalError
from telegram import InlineKeyboardMarkup, Message, Update, Voice
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes

from app import bot
from app.bot import (
    BROKEN_REDO,
    BUSY,
    EMPTY,
    FIRST_STAGE,
    GATE_EDIT_ASKED,
    GATE_STOPPED,
    GATE_TAIL,
    LOST,
    GREETING,
    HEARD,
    LABEL,
    MAX_VOICE_SECONDS,
    ASK_AGAIN,
    NOTHING_HEARD,
    PICK_ONE,
    STALE_BUTTON,
    VOICE_INGEST_LABEL,
    VOICE_NOT_TAKEN,
    allowed_chats,
    cards_published,
    chosen_edit,
    finished_text,
    follow,
    main,
    on_choice_button,
    on_gate_button,
    on_start,
    on_text,
    on_voice,
    outcome,
    progress_text,
    refuse,
    GATES_FROM_NEXT_RUN,
    GATES_STATE,
    GATES_UNKNOWN,
    auto_approve_for,
    on_gates,
    started_run,
    too_long,
)
from app.candidates import parse_candidates
from app.config import ConfigError, MissingApiKey, settings
from app.pipeline import (
    BRIEF,
    CANDIDATES,
    ISSUES_JSON,
    ISSUES_MD,
    NAMES,
    Stage,
    StopKind,
    stages_between,
)
from app.run import Pause, Redo, Run, walk
from app.store import (
    AWAITING_GATE,
    DROPPED,
    FAILED,
    NO_TASK,
    PUBLISHED,
    STATUS_OF_STOP,
    Stopped,
)
from tests.helpers import REAL_BRIEF, REAL_ISSUES
from tests.test_candidates import MULTIPLE, NONE, NONE_EMPTY


class FakeStore:
    """Строка прогона в памяти теста: те же вызовы, что бот делает к базе.

    Настоящую базу трогают только тесты `app/store.py` (TESTING.md): поднимать Postgres ради
    проверки обработчика значит проверять две вещи разом и падать по второй.
    """

    def __init__(self) -> None:
        self.stops: dict[int, Stopped] = {}
        self.chats: dict[str, int] = {}
        self.status: dict[str, str] = {}
        self.sources: dict[str, str] = {}
        self.approved: dict[str, bool] = {}
        self.started: list[tuple[str, int, str]] = []

    def stop(self, chat_id: int, stopped: Stopped) -> None:
        self.stops[chat_id] = stopped
        self.chats[stopped.run_id] = chat_id
        # Источник и режим ворот — колонки строки, а не свойства остановки: она их переживает.
        self.sources.setdefault(stopped.run_id, stopped.source)
        self.approved.setdefault(stopped.run_id, stopped.auto_approve)

    def waiting_for(self, chat_id: int) -> Stopped | None:
        return self.stops.get(chat_id)

    def start_run(
        self, run_id: str, chat_id: int, source: str, lang: str, auto_approve: bool
    ) -> None:
        self.started.append((run_id, chat_id, source))
        self.chats[run_id] = chat_id
        self.status[run_id] = NAMES[0]
        self.sources[run_id] = source
        self.approved[run_id] = auto_approve

    def mark_stage(self, run_id: str, status: str, lang: str) -> None:
        self.status[run_id] = status

    def stop_run(self, run_id: str, kind: StopKind, stage: str, artifact: str) -> None:
        self.status[run_id] = STATUS_OF_STOP[kind]
        self.stop(
            self.chats[run_id],
            Stopped(
                run_id=run_id,
                lang="ru",
                source=self.sources[run_id],
                auto_approve=self.approved[run_id],
                kind=kind,
                stage=stage,
                artifact=artifact,
            ),
        )

    def finish_run(self, run_id: str, status: str) -> None:
        self.status[run_id] = status
        self.stops.pop(self.chats.get(run_id, 0), None)

    def drop_stop(self, chat_id: int) -> str | None:
        left = self.stops.pop(chat_id, None)
        if left is None:
            return None
        self.status[left.run_id] = DROPPED
        return left.run_id


@pytest.fixture(autouse=True)
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    fake = FakeStore()
    for name in (
        "waiting_for",
        "start_run",
        "mark_stage",
        "stop_run",
        "finish_run",
        "drop_stop",
    ):
        monkeypatch.setattr(bot, name, getattr(fake, name))
    return fake


def a_run(run_id: str = "прогон", audio: Path | None = None) -> Run:
    return Run(root=Path("."), run_id=run_id, lang="ru", audio=audio, auto_approve=True)


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
    text = progress_text(a_run(), [], voice=False)

    printed = [line[2:] for line in text.splitlines()[2:]]
    assert printed == [LABEL[name] for name in NAMES]


def test_the_progress_marks_what_is_done_and_what_runs_now() -> None:
    text = progress_text(a_run(), ["ingest", "intake"], voice=False)

    marks = [line[0] for line in text.splitlines()[2:]]
    assert marks == ["✓", "✓", "▸", "·", "·", "·", "·"]


def test_a_finished_walk_marks_everything_and_points_at_nothing() -> None:
    text = progress_text(a_run(), list(NAMES), voice=False)

    assert [line[0] for line in text.splitlines()[2:]] == ["✓"] * len(NAMES)


def test_the_progress_carries_the_run_id_so_the_log_can_be_found() -> None:
    assert progress_text(a_run("a1b2c3d4"), [], voice=False).startswith("Прогон a1b2c3d4")


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


def test_a_new_run_takes_the_gates_from_the_settings_and_not_from_the_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ворота — норма (CLAUDE.md §1), а стенд снимает их своим .env, а не умолчанием кода."""
    assert not started_run("прогон", 12, text="Идея").auto_approve

    monkeypatch.setattr(settings, "auto_approve", True)

    assert started_run("прогон", 12, text="Идея").auto_approve


def test_a_voice_at_the_limit_runs_and_a_second_over_it_does_not() -> None:
    assert not too_long(a_voice(MAX_VOICE_SECONDS))
    assert too_long(a_voice(MAX_VOICE_SECONDS + 1))


def test_the_progress_calls_the_first_step_transcription_for_a_voice_run() -> None:
    printed = progress_text(a_run(), [], voice=True).splitlines()[2:]

    assert printed[0] == f"▸ {VOICE_INGEST_LABEL}"
    assert printed[1:] == [f"· {LABEL[name]}" for name in NAMES[1:]]


@contextmanager
def database_of_nobody() -> Iterator[bool]:
    yield True


def ready_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Всё, без чего бот не поднимется. Каждый тест старта ломает ровно одно.

    База здесь подменена: тест про однопрогонность спрашивает у `main` про очередь апдейтов, а
    добирался до настоящего Postgres и падал на его блокировке, стоило поднять бота рядом.
    Настоящую базу трогают только тесты `app/store.py`, помеченные `db` (TESTING.md).
    """
    monkeypatch.setattr(settings, "telegram_bot_token", "1:token")
    monkeypatch.setattr(settings, "allow_live_api", True)
    monkeypatch.setattr(settings, "openai_api_key", "test")
    listed(monkeypatch, "12")
    monkeypatch.setattr(bot, "ffmpeg_installed", lambda: True)
    monkeypatch.setattr(bot, "ensure_schema", lambda: None)
    monkeypatch.setattr(bot, "one_bot_per_database", database_of_nobody)


def test_the_bot_does_not_start_without_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Проверка на старте, а не на первом голосовом: у стенда это уже поздно."""
    ready_to_start(monkeypatch)
    monkeypatch.setattr(bot, "ffmpeg_installed", lambda: False)

    with pytest.raises(ConfigError, match="ffmpeg"):
        main()


def test_the_bot_does_not_start_without_the_key_that_transcribes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Тот же довод, что и у ffmpeg: без ключа голосовое доедет до «прогон сорвался»."""
    ready_to_start(monkeypatch)
    monkeypatch.setattr(settings, "openai_api_key", "")

    with pytest.raises(MissingApiKey, match="OPENAI_API_KEY"):
        main()


class SlowNote:
    """Сообщение, у которого правка прогресса отвечает медленнее финальной.

    Ровно так прогон 0ac7bdffe0e0ba58 и потерял ссылку: ответ на последнюю правку прогресса
    пришёл после ответа на правку со ссылкой и затёр её списком галочек. При одинаковых
    задержках порядок сохраняется сам собой, и тест проходит даже на сломанном коде.
    """

    def __init__(self) -> None:
        self.edits: list[str] = []

    async def edit_text(self, text: str, reply_markup: object = None) -> Message:
        await asyncio.sleep(0.05 if text.startswith("Прогон") else 0)
        self.edits.append(text)
        return cast(Message, self)


# Двойники обхода объявлены типом настоящего `walk`, а не `Callable[..., Pause | None]`:
# со стёртой сигнатурой её смена проходила мимо mypy и всплывала шестью красными тестами.
Walking = Callable[[Run, str, str, Callable[[Stage], None], Redo | None], Pause | None]
walk_itself: Walking = walk


def walk_reporting_every_stage(
    run: Run, start: str, stop: str, on_done: Callable[[Stage], None], redo: Redo | None = None
) -> Pause | None:
    for stage in stages_between(start, stop):
        on_done(stage)
    return None


@pytest.mark.asyncio
async def test_the_link_is_the_last_thing_the_message_shows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
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
        datetime.now(timezone.utc),
    )

    assert note.edits[-1] == finished_text(tmp_path)
    assert len(note.edits) == len(NAMES) + 1
    assert store.status["прогон"] == PUBLISHED


def walk_breaking(
    run: Run, start: str, stop: str, on_done: Callable[[Stage], None], redo: Redo | None = None
) -> Pause | None:
    raise RuntimeError("стадия не ответила")


def walk_stopping_on_choice(
    run: Run, start: str, stop: str, on_done: Callable[[Stage], None], redo: Redo | None = None
) -> Pause | None:
    return Pause(stage="intake", artifact=CANDIDATES, kind="choice")


def a_stopped_run(root: Path, monkeypatch: pytest.MonkeyPatch, candidates: str) -> Run:
    monkeypatch.setattr(bot, "walk", walk_stopping_with(candidates))
    return Run(root=root, run_id="прогон", lang="ru", text="…", auto_approve=True)


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


def test_the_bot_answers_while_a_run_is_walking(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без этого флага PTB берёт следующее обновление только после конца прогона.

    Отказ «прогон уже идёт» тогда недостижим, и три сообщения подряд становятся тремя
    оплаченными прогонами вместо одного и двух отказов.
    """
    started: list[Application[Any, Any, Any, Any, Any, Any]] = []
    ready_to_start(monkeypatch)
    monkeypatch.setattr(Application, "run_polling", lambda self, **kwargs: started.append(self))

    main()

    assert started[0].concurrent_updates > 1


@pytest.mark.asyncio
async def test_a_finished_run_reports_how_long_the_person_waited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Репетиция (P2-06) спрашивает «сколько ждёт человек», и ответ идёт от отправки сообщения."""
    journal = tmp_path / "outputs/publish.json"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"I-001": {}, "I-002": {}}', encoding="utf-8")
    monkeypatch.setattr(settings, "trello_board_id", "board1")
    monkeypatch.setattr(bot, "walk", walk_reporting_every_stage)

    with caplog.at_level("INFO", logger="app.bot"):
        await follow(
            cast(Message, SlowNote()),
            Run(root=tmp_path, run_id="прогон", lang="ru", text="Идея", auto_approve=True),
            datetime.now(timezone.utc) - timedelta(seconds=42),
        )

    assert "run=прогон finished cards=2" in caplog.text
    assert "run=прогон seconds=42" in caplog.text


@pytest.mark.asyncio
async def test_the_stop_after_intake_shows_what_the_model_heard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """До кнопок (P3-07) человек выбирает сам, поэтому обязан видеть, между чем."""
    run = a_stopped_run(tmp_path, monkeypatch, MULTIPLE)

    said = (await outcome(run, lambda stage: None, FIRST_STAGE, None)).text

    assert "1. Бот для онбординга новичков" in said
    assert "2. Утренняя сводка по просроченным дедлайнам" in said
    assert said.endswith(PICK_ONE)


@pytest.mark.asyncio
async def test_a_missing_candidates_file_names_the_cause_it_knows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Чтение артефакта идёт после обхода: без перехвата человек остался бы без концовки."""
    monkeypatch.setattr(bot, "walk", walk_stopping_on_choice)
    run = Run(root=tmp_path, run_id="прогон", lang="ru", text="…", auto_approve=True)

    said = (await outcome(run, lambda stage: None, FIRST_STAGE, None)).text

    assert said == LOST.format(run_id="прогон")


@pytest.mark.asyncio
async def test_a_run_that_found_no_task_says_so_and_still_shows_what_was_discussed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Тот же список в том же файле, но это темы разговора: подать их как идеи значит соврать."""
    run = a_stopped_run(tmp_path, monkeypatch, NONE)

    said = (await outcome(run, lambda stage: None, FIRST_STAGE, None)).text

    assert said.startswith(NOTHING_HEARD)
    assert "1. Сроки по текущему спринту" in said
    assert said.endswith(ASK_AGAIN)


@pytest.mark.asyncio
async def test_a_recording_with_nothing_in_it_gets_the_lead_and_the_ask_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пустой список — не пустая строка в чате: показывать нечего, и показывать нечего."""
    run = a_stopped_run(tmp_path, monkeypatch, NONE_EMPTY)

    said = (await outcome(run, lambda stage: None, FIRST_STAGE, None)).text

    assert said == f"{NOTHING_HEARD}\n\n{ASK_AGAIN}"


class TextChat(QuietChat):
    """Текстовое сообщение из чата 12: обработчику хватает текста, времени и ответов."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text
        self.date = datetime.now(timezone.utc)
        self.edits: list[str] = []
        self.keyboards: list[object] = []
        self.documents: list[Path] = []
        self.document_fails = False

    async def edit_text(self, text: str, reply_markup: object = None) -> Message:
        self.edits.append(text)
        self.keyboards.append(reply_markup)
        return cast(Message, self)

    async def reply_document(self, document: Path) -> Message:
        if self.document_fails:
            raise TelegramError("файл не ушёл")
        self.documents.append(document)
        return cast(Message, self)


class VoiceChat(TextChat):
    def __init__(self, voice: Voice) -> None:
        super().__init__("")
        self.voice = voice


def an_update(message: object) -> Update:
    return cast(Update, SimpleNamespace(message=message))


NO_CONTEXT = cast(ContextTypes.DEFAULT_TYPE, None)


def a_stopped_choice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidates: str = MULTIPLE,
    auto_approve: bool = True,
) -> Stopped:
    """Остановка, пережившая процесс: строка помнит место, список лежит файлом в прогоне."""
    monkeypatch.setattr(bot, "RUNS", tmp_path / "runs")
    root = tmp_path / "runs" / "прогон"
    (root / "outputs").mkdir(parents=True, exist_ok=True)
    (root / CANDIDATES).write_text(candidates, encoding="utf-8")
    return Stopped(
        run_id="прогон",
        lang="ru",
        source="voice",
        auto_approve=auto_approve,
        kind="choice",
        stage="intake",
        artifact=CANDIDATES,
    )


def walk_recording(seen: list[tuple[str, str, Redo | None]]) -> Walking:
    def walking(
        run: Run, start: str, stop: str, on_done: Callable[[Stage], None], redo: Redo | None = None
    ) -> Pause | None:
        seen.append((run.run_id, start, redo))
        (run.root / "outputs").mkdir(parents=True, exist_ok=True)
        (run.root / "outputs/publish.json").write_text('{"I-001": {}}', encoding="utf-8")
        return None

    return walking


@pytest.mark.asyncio
async def test_the_answer_after_a_choice_continues_the_same_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore) -> None:
    """Расшифровка уже есть: новый прогон означал бы просьбу надиктовать идею заново."""
    listed(monkeypatch, "12")
    store.stop(12, a_stopped_choice(tmp_path, monkeypatch))
    seen: list[tuple[str, str, Redo | None]] = []
    monkeypatch.setattr(bot, "walk", walk_recording(seen))

    chat = TextChat("2")

    await on_text(an_update(chat), NO_CONTEXT)

    assert seen == [
        (
            "прогон",
            "intake",
            Redo(
                kind="choice",
                user_edit="Выбрана идея 2: Утренняя сводка по просроченным дедлайнам",
                artifact=CANDIDATES,
            ),
        )
    ]
    assert 12 not in store.stops
    assert chat.replies[0].splitlines()[2] == f"✓ {VOICE_INGEST_LABEL}"


@pytest.mark.asyncio
async def test_a_number_outside_the_list_keeps_the_run_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore) -> None:
    """Прогон не сбрасывается из-за опечатки: человек ещё выбирает."""
    listed(monkeypatch, "12")
    stopped = a_stopped_choice(tmp_path, monkeypatch)
    store.stop(12, stopped)
    seen: list[tuple[str, str, Redo | None]] = []
    monkeypatch.setattr(bot, "walk", walk_recording(seen))
    chat = TextChat("7")

    await on_text(an_update(chat), NO_CONTEXT)

    assert chat.replies == ["Идей всего 2. Пришлите номер от 1 до 2 или саму идею словами."]
    assert store.stops[12] == stopped
    assert seen == []


@pytest.mark.asyncio
async def test_the_stop_is_answered_from_the_file_the_run_keeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """Список живёт файлом в прогоне: строка помнит только место, куда возвращаться."""
    listed(monkeypatch, "12")
    store.stop(12, a_stopped_choice(tmp_path, monkeypatch))
    seen: list[tuple[str, str, Redo | None]] = []
    monkeypatch.setattr(bot, "walk", walk_recording(seen))

    await on_text(an_update(TextChat("2")), NO_CONTEXT)

    assert [start for _, start, _ in seen] == ["intake"]


@pytest.mark.asyncio
async def test_a_stop_whose_file_is_gone_says_so_and_closes_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """`make clean-runs` между репетициями уносит расшифровку: повторять нечего, ждать тоже."""
    listed(monkeypatch, "12")
    stopped = a_stopped_choice(tmp_path, monkeypatch)
    store.stop(12, stopped)
    (tmp_path / "runs" / "прогон" / CANDIDATES).unlink()
    seen: list[tuple[str, str, Redo | None]] = []
    monkeypatch.setattr(bot, "walk", walk_recording(seen))
    chat = TextChat("2")

    await on_text(an_update(chat), NO_CONTEXT)

    assert chat.replies == [LOST.format(run_id="прогон")]
    assert store.status["прогон"] == FAILED
    assert seen == []


@pytest.mark.asyncio
async def test_a_digit_that_is_no_number_goes_to_the_stage_instead_of_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore) -> None:
    """У «²» isdigit истинен, а int падает: обработчик срывался, и человек не получал ничего."""
    listed(monkeypatch, "12")
    store.stop(12, a_stopped_choice(tmp_path, monkeypatch))
    seen: list[tuple[str, str, Redo | None]] = []
    monkeypatch.setattr(bot, "walk", walk_recording(seen))

    await on_text(an_update(TextChat("²")), NO_CONTEXT)

    assert [redo.user_edit for _, _, redo in seen if redo] == ["²"]


@pytest.mark.asyncio
async def test_an_empty_message_is_refused_and_the_stop_stays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore) -> None:
    """Пробел снимал остановку и уходил в стадию правкой без единого слова."""
    listed(monkeypatch, "12")
    stopped = a_stopped_choice(tmp_path, monkeypatch)
    store.stop(12, stopped)
    seen: list[tuple[str, str, Redo | None]] = []
    monkeypatch.setattr(bot, "walk", walk_recording(seen))
    chat = TextChat("   ")

    await on_text(an_update(chat), NO_CONTEXT)

    assert chat.replies == [EMPTY]
    assert store.stops[12] == stopped
    assert seen == []


@pytest.mark.asyncio
async def test_a_broken_redo_keeps_the_recording_and_the_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore) -> None:
    """Иначе сорванная стадия теряет расшифровку — та самая потеря, ради которой был P3-04."""
    listed(monkeypatch, "12")
    stopped = a_stopped_choice(tmp_path, monkeypatch)
    store.stop(12, stopped)

    monkeypatch.setattr(bot, "walk", walk_breaking)
    chat = TextChat("2")

    await on_text(an_update(chat), NO_CONTEXT)

    assert chat.edits[-1] == BROKEN_REDO["choice"].format(run_id="прогон")
    assert store.stops[12] == stopped


@pytest.mark.asyncio
async def test_a_run_whose_files_are_gone_drops_the_stop_instead_of_looping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore) -> None:
    """`make clean-runs` между репетициями уносит расшифровку: повторять станет нечего.

    Остановка при этом копила бы один и тот же отказ на каждый следующий ответ человека.
    """
    listed(monkeypatch, "12")
    store.stop(12, a_stopped_choice(tmp_path, monkeypatch))

    def walk_without_files(
        run: Run, start: str, stop: str, on_done: Callable[[Stage], None], redo: Redo | None = None
    ) -> Pause | None:
        raise FileNotFoundError(run.root / "inputs/transcript.md")

    monkeypatch.setattr(bot, "walk", walk_without_files)
    chat = TextChat("1")

    await on_text(an_update(chat), NO_CONTEXT)

    assert chat.edits[-1] == LOST.format(run_id="прогон")
    assert 12 not in store.stops


@pytest.mark.asyncio
async def test_the_answer_to_a_choice_leaves_a_line_to_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    store: FakeStore,
) -> None:
    """Отчёт репетиции (P2-06) считает воронку grep-ом: `stop=choice` был, ответов на него нет."""
    listed(monkeypatch, "12")
    store.stop(12, a_stopped_choice(tmp_path, monkeypatch))
    seen: list[tuple[str, str, Redo | None]] = []
    monkeypatch.setattr(bot, "walk", walk_recording(seen))

    with caplog.at_level(logging.INFO, logger="app.bot"):
        await on_text(an_update(TextChat("1")), NO_CONTEXT)

    assert "answer=choice by=text run=прогон chat=12" in caplog.text


@pytest.mark.asyncio
async def test_start_waits_for_the_run_so_its_reset_is_not_written_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """`/start` во время прогона снимал пустоту: остановку прогон записывал уже после него."""
    listed(monkeypatch, "12")
    store.stop(12, a_stopped_choice(tmp_path, monkeypatch))
    chat = TextChat("/start")
    await bot.running.acquire()
    started = asyncio.create_task(on_start(an_update(chat), NO_CONTEXT))
    await asyncio.sleep(0.01)
    waited = chat.replies == [] and 12 in store.stops
    bot.running.release()
    await started

    assert waited
    assert 12 not in store.stops
    assert chat.replies == [GREETING]


async def saved_empty(voice: Voice, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"")


@pytest.mark.asyncio
async def test_a_voice_always_starts_a_new_run_and_forgets_the_stopped_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore) -> None:
    """На стенде следующий человек говорит голосом: его запись — не правка к чужому выбору."""
    listed(monkeypatch, "12")
    store.stop(12, a_stopped_choice(tmp_path, monkeypatch))
    monkeypatch.setattr(bot, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(bot, "save_voice", saved_empty)
    seen: list[tuple[str, str, Redo | None]] = []
    monkeypatch.setattr(bot, "walk", walk_recording(seen))

    await on_voice(an_update(VoiceChat(a_voice(3))), NO_CONTEXT)

    assert 12 not in store.stops
    assert store.status["прогон"] == DROPPED
    assert [(start, redo) for _, start, redo in seen] == [("ingest", None)]


@pytest.mark.asyncio
async def test_a_database_that_breaks_mid_run_does_not_take_the_cards_away(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """Отметка о стадии — не работа прогона: артефакты на диске, карточки на доске."""
    journal = tmp_path / "outputs/publish.json"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"I-001": {}}', encoding="utf-8")
    monkeypatch.setattr(settings, "trello_board_id", "board1")
    monkeypatch.setattr(bot, "walk", walk_reporting_every_stage)

    def broken(*args: object, **kwargs: object) -> None:
        raise OperationalError("select 1", {}, Exception("база ушла"))

    monkeypatch.setattr(bot, "mark_stage", broken)
    monkeypatch.setattr(bot, "finish_run", broken)
    chat = TextChat("Идея")

    await follow(
        cast(Message, chat),
        Run(root=tmp_path, run_id="прогон", lang="ru", text="Идея", auto_approve=True),
        datetime.now(timezone.utc),
    )

    assert chat.edits[-1] == finished_text(tmp_path)


@pytest.mark.asyncio
async def test_a_voice_that_never_arrives_closes_its_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """Незакрытая строка осталась бы в рабочем статусе, и следующий старт сказал бы человеку,
    что прерван прогон, которого не было."""
    listed(monkeypatch, "12")
    monkeypatch.setattr(bot, "RUNS", tmp_path / "runs")

    async def not_downloaded(voice: Voice, target: Path) -> None:
        raise TelegramError("сеть отвалилась")

    monkeypatch.setattr(bot, "save_voice", not_downloaded)
    chat = VoiceChat(a_voice(3))

    await on_voice(an_update(chat), NO_CONTEXT)

    assert chat.edits[-1] == VOICE_NOT_TAKEN
    assert store.status[store.started[0][0]] == FAILED


@pytest.mark.asyncio
async def test_a_choice_after_a_voice_is_remembered_as_after_a_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore) -> None:
    """Живой прогон 664534620c9a1c10: остановку записывал только on_text, а голос её терял.

    На стенде идею наговаривают, поэтому «1» после голосового уходило новым прогоном —
    из одного слова, на чужом языке и без идеи внутри.
    """
    listed(monkeypatch, "12")
    monkeypatch.setattr(bot, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(bot, "save_voice", saved_empty)
    monkeypatch.setattr(bot, "walk", walk_stopping_with(MULTIPLE))

    await on_voice(an_update(VoiceChat(a_voice(3))), NO_CONTEXT)

    assert store.stops[12].stage == "intake"
    assert store.stops[12].artifact == CANDIDATES


@pytest.mark.asyncio
async def test_start_drops_a_stopped_run_so_a_new_idea_can_begin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """Выхода из остановки больше нет: текст — это выбор, а голосовое есть не у всех."""
    listed(monkeypatch, "12")
    store.stop(12, a_stopped_choice(tmp_path, monkeypatch))
    chat = TextChat("/start")

    await on_start(an_update(chat), NO_CONTEXT)

    assert 12 not in store.stops
    assert chat.replies == [GREETING]


def test_a_continued_run_takes_its_facts_from_the_row_and_not_from_the_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Настройка могла смениться между сообщением человека и его ответом: прогон, начатый с
    воротами, обязан с ними и кончиться, а язык голосовому назвал Whisper, а не DEFAULT_LANG."""
    monkeypatch.setattr(settings, "auto_approve", True)
    monkeypatch.setattr(settings, "default_lang", "de")
    stopped = a_stopped_choice(tmp_path, monkeypatch, auto_approve=False)

    run = bot.continued(stopped)

    assert not run.auto_approve
    assert run.lang == "ru"


def test_a_number_becomes_the_choice_the_stage_can_read() -> None:
    found = parse_candidates(MULTIPLE)

    assert chosen_edit(" 1 ", found) == "Выбрана идея 1: Бот для онбординга новичков"


def test_anything_but_a_number_goes_to_the_stage_as_it_was_written() -> None:
    """Порог «сколько букв уже новая идея» выдумывать нечем: любой текст — правка."""
    found = parse_candidates(MULTIPLE)

    assert chosen_edit("Первую, но только про доступы", found) == "Первую, но только про доступы"


def test_a_number_that_is_not_in_the_list_is_not_a_choice() -> None:
    assert chosen_edit("7", parse_candidates(MULTIPLE)) is None


def walk_stopping_with(candidates: str) -> Walking:
    def walking(
        run: Run, start: str, stop: str, on_done: Callable[[Stage], None], redo: Redo | None = None
    ) -> Pause | None:
        (run.root / "outputs").mkdir(parents=True, exist_ok=True)
        (run.root / CANDIDATES).write_text(candidates, encoding="utf-8")
        return Pause(stage="intake", artifact=CANDIDATES, kind="choice")

    return walking


@pytest.mark.asyncio
async def test_a_choice_is_remembered_so_the_next_message_can_answer_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore) -> None:
    listed(monkeypatch, "12")
    monkeypatch.setattr(bot, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(bot, "walk", walk_stopping_with(MULTIPLE))
    chat = TextChat("Две идеи разом")

    await on_text(an_update(chat), NO_CONTEXT)

    assert store.stops[12].stage == "intake"
    assert store.stops[12].artifact == CANDIDATES
    assert chat.edits[-1].startswith(HEARD)


@pytest.mark.asyncio
async def test_a_recording_with_nothing_in_it_is_not_worth_waiting_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore) -> None:
    """Ждать ответа не на что: в записи не было ничего, и правка пришлась бы к пустому."""
    listed(monkeypatch, "12")
    monkeypatch.setattr(bot, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(bot, "walk", walk_stopping_with(NONE_EMPTY))
    chat = TextChat("Эээ")

    await on_text(an_update(chat), NO_CONTEXT)

    assert store.stops == {}
    assert chat.edits[-1].startswith(NOTHING_HEARD)
    assert store.status[store.started[0][0]] == NO_TASK


@pytest.mark.asyncio
async def test_the_choice_message_carries_a_button_per_idea(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """Номер на кнопке — тот же, что в списке: она отвечает ровно то, что человек набрал бы сам."""
    listed(monkeypatch, "12")
    monkeypatch.setattr(bot, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(bot, "walk", walk_stopping_with(MULTIPLE))
    chat = TextChat("Две идеи разом")

    await on_text(an_update(chat), NO_CONTEXT)

    keyboard = chat.keyboards[-1]
    assert isinstance(keyboard, InlineKeyboardMarkup)
    run_id = store.started[0][0]
    assert [(button.text, button.callback_data) for button in keyboard.inline_keyboard[0]] == [
        ("1", f"pick:{run_id}:1"),
        ("2", f"pick:{run_id}:2"),
    ]


@pytest.mark.asyncio
async def test_a_list_with_nothing_to_choose_from_carries_no_buttons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """При `none` список — темы разговора, а не идеи, и отвечать нажатием не на что."""
    listed(monkeypatch, "12")
    monkeypatch.setattr(bot, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(bot, "walk", walk_stopping_with(NONE))
    chat = TextChat("Поговорили ни о чём")

    await on_text(an_update(chat), NO_CONTEXT)

    assert chat.edits[-1].startswith(NOTHING_HEARD)
    assert chat.keyboards[-1] is None


class ButtonChat(TextChat):
    """Нажатая кнопка: сам callback и сообщение, на котором она висела."""

    def __init__(self, data: str) -> None:
        super().__init__("")
        self.data = data
        self.answered = 0
        self.markups: list[object] = []

    async def answer(self, text: str | None = None) -> bool:
        self.answered += 1
        return True

    async def edit_message_reply_markup(self, reply_markup: object = None) -> Message:
        self.markups.append(reply_markup)
        return cast(Message, self)


def a_gate_button(run_id: str, decision: str, stage: str = "brief") -> ButtonChat:
    return ButtonChat(f"gate:{run_id}:{stage}:{decision}")


def a_choice_button(run_id: str, number: str) -> ButtonChat:
    return ButtonChat(f"pick:{run_id}:{number}")


def a_press(chat: ButtonChat) -> Update:
    # `effective_message` у настоящего Update — свойство; двойник отдаёт то же самое полем.
    return cast(Update, SimpleNamespace(callback_query=chat, effective_message=chat))


def a_stopped_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str = "brief",
    artifact: str = BRIEF,
    source: str = "text",
    auto_approve: bool = False,
) -> Stopped:
    """Прогон, ждущий решения на воротах: артефакт лежит файлом, место помнит строка."""
    monkeypatch.setattr(bot, "RUNS", tmp_path / "runs")
    root = tmp_path / "runs" / "прогон"
    (root / "outputs").mkdir(parents=True, exist_ok=True)
    (root / artifact).write_text(REAL_BRIEF, encoding="utf-8")
    return Stopped(
        run_id="прогон",
        lang="ru",
        source=source,
        auto_approve=auto_approve,
        kind="gate",
        stage=stage,
        artifact=artifact,
    )


def walk_stopping_at_a_gate(stage: str, artifact: str, files: dict[str, str]) -> Walking:
    def walking(
        run: Run, start: str, stop: str, on_done: Callable[[Stage], None], redo: Redo | None = None
    ) -> Pause | None:
        (run.root / "outputs").mkdir(parents=True, exist_ok=True)
        for path, content in files.items():
            (run.root / path).write_text(content, encoding="utf-8")
        return Pause(stage=stage, artifact=artifact, kind="gate")

    return walking


@pytest.mark.asyncio
async def test_a_gate_shows_the_numbers_the_artifact_gives_and_sends_it_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """Подтвердить то, чего не видел, — не ворота: в сообщении числа, файл идёт следом."""
    listed(monkeypatch, "12")
    monkeypatch.setattr(bot, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(bot, "walk", walk_stopping_at_a_gate("brief", BRIEF, {BRIEF: REAL_BRIEF}))
    chat = TextChat("Идея")

    await on_text(an_update(chat), NO_CONTEXT)

    run_id = store.started[0][0]
    assert chat.edits[-1] == (
        f"Бриф готов: Бот для анбординга новичков\nОткрытых вопросов: 15.\n\n{GATE_TAIL}"
    )
    assert chat.documents == [tmp_path / "runs" / run_id / BRIEF]
    assert store.stops[12].stage == "brief"
    assert store.status[run_id] == AWAITING_GATE


@pytest.mark.asyncio
async def test_the_gate_message_carries_the_three_buttons_of_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """Прогон назван в самой кнопке: вчерашняя не должна двигать сегодняшний."""
    listed(monkeypatch, "12")
    monkeypatch.setattr(bot, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(bot, "walk", walk_stopping_at_a_gate("brief", BRIEF, {BRIEF: REAL_BRIEF}))
    chat = TextChat("Идея")

    await on_text(an_update(chat), NO_CONTEXT)

    keyboard = chat.keyboards[-1]
    assert isinstance(keyboard, InlineKeyboardMarkup)
    pressed = [(button.text, button.callback_data) for button in keyboard.inline_keyboard[0]]
    run_id = store.started[0][0]
    assert pressed == [
        ("Дальше", f"gate:{run_id}:brief:next"),
        ("Править", f"gate:{run_id}:brief:edit"),
        ("Стоп", f"gate:{run_id}:brief:stop"),
    ]


@pytest.mark.asyncio
async def test_next_at_a_gate_continues_from_the_stage_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """Подтверждённую стадию переигрывать значит оплатить её второй раз и получить другое."""
    listed(monkeypatch, "12")
    store.stop(12, a_stopped_gate(tmp_path, monkeypatch))
    seen: list[tuple[str, str, Redo | None]] = []
    monkeypatch.setattr(bot, "walk", walk_recording(seen))
    chat = a_gate_button("прогон", "next")

    await on_gate_button(a_press(chat), NO_CONTEXT)

    assert seen == [("прогон", "research", None)]
    assert chat.markups == [None]
    assert store.status["прогон"] == PUBLISHED


@pytest.mark.asyncio
async def test_the_edit_button_asks_for_text_and_leaves_the_gate_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """Кнопки не снимаются: человек может передумать и подтвердить как есть."""
    listed(monkeypatch, "12")
    stopped = a_stopped_gate(tmp_path, monkeypatch)
    store.stop(12, stopped)
    seen: list[tuple[str, str, Redo | None]] = []
    monkeypatch.setattr(bot, "walk", walk_recording(seen))
    chat = a_gate_button("прогон", "edit")

    await on_gate_button(a_press(chat), NO_CONTEXT)

    assert chat.replies == [GATE_EDIT_ASKED]
    assert chat.markups == []
    assert store.stops[12] == stopped
    assert seen == []


@pytest.mark.asyncio
async def test_the_text_after_a_gate_goes_back_into_the_same_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """Правка — про артефакт этой стадии, поэтому обход возвращается ровно в неё."""
    listed(monkeypatch, "12")
    store.stop(12, a_stopped_gate(tmp_path, monkeypatch))
    seen: list[tuple[str, str, Redo | None]] = []
    monkeypatch.setattr(bot, "walk", walk_recording(seen))

    await on_text(an_update(TextChat("Убери пятый раздел")), NO_CONTEXT)

    assert seen == [
        ("прогон", "brief", Redo(kind="gate", user_edit="Убери пятый раздел", artifact=BRIEF))
    ]


@pytest.mark.asyncio
async def test_stop_at_a_gate_ends_the_run_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """«Стоп» — тот же выход из остановки, что голосовое и `/start`."""
    listed(monkeypatch, "12")
    store.stop(12, a_stopped_gate(tmp_path, monkeypatch))
    seen: list[tuple[str, str, Redo | None]] = []
    monkeypatch.setattr(bot, "walk", walk_recording(seen))
    chat = a_gate_button("прогон", "stop")

    await on_gate_button(a_press(chat), NO_CONTEXT)

    assert chat.replies == [GATE_STOPPED.format(run_id="прогон")]
    assert 12 not in store.stops
    assert store.status["прогон"] == DROPPED
    assert seen == []


@pytest.mark.asyncio
async def test_a_button_of_a_run_that_no_longer_waits_moves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """Сообщение с воротами живёт в чате вечно, а остановку снимают голосовое, `/start` и «Стоп»."""
    listed(monkeypatch, "12")
    store.stop(12, a_stopped_gate(tmp_path, monkeypatch))
    seen: list[tuple[str, str, Redo | None]] = []
    monkeypatch.setattr(bot, "walk", walk_recording(seen))
    chat = a_gate_button("вчерашний", "next")

    await on_gate_button(a_press(chat), NO_CONTEXT)

    assert chat.replies == [STALE_BUTTON]
    assert seen == []
    assert store.stops[12].run_id == "прогон"


@pytest.mark.asyncio
async def test_a_button_pressed_during_a_run_is_refused_like_a_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Отказ на кнопке называет прогон: он лежит в самой кнопке, а по логу считают воронку."""
    listed(monkeypatch, "12")
    store.stop(12, a_stopped_gate(tmp_path, monkeypatch))
    chat = a_gate_button("прогон", "next")
    await bot.running.acquire()
    try:
        with caplog.at_level(logging.INFO, logger="app.bot"):
            await on_gate_button(a_press(chat), NO_CONTEXT)
    finally:
        bot.running.release()

    assert chat.replies == [BUSY]
    assert chat.answered == 1
    assert "refusal=busy chat=12 run=прогон" in caplog.text


@pytest.mark.asyncio
async def test_a_broken_gate_redo_asks_for_the_edit_again_and_keeps_the_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """Остановка снимается только удачным повтором: иначе прогон теряется на первом сбое."""
    listed(monkeypatch, "12")
    stopped = a_stopped_gate(tmp_path, monkeypatch)
    store.stop(12, stopped)
    monkeypatch.setattr(bot, "walk", walk_breaking)
    chat = TextChat("Убери пятый раздел")

    await on_text(an_update(chat), NO_CONTEXT)

    assert chat.edits[-1] == BROKEN_REDO["gate"].format(run_id="прогон")
    assert store.stops[12] == stopped
    assert store.status["прогон"] == AWAITING_GATE


@pytest.mark.asyncio
async def test_a_gate_after_decompose_counts_the_backlog_it_holds_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """Читает человек issues.md, а числа берутся из issues.json: их там не пересказывают."""
    listed(monkeypatch, "12")
    monkeypatch.setattr(bot, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(
        bot,
        "walk",
        walk_stopping_at_a_gate(
            "decompose", ISSUES_MD, {ISSUES_JSON: REAL_ISSUES, ISSUES_MD: "# Backlog\n"}
        ),
    )

    chat = TextChat("Идея")
    await on_text(an_update(chat), NO_CONTEXT)

    run_id = store.started[0][0]
    assert chat.edits[-1].startswith("Бэклог готов: ")
    assert chat.documents == [tmp_path / "runs" / run_id / ISSUES_MD]


@pytest.mark.asyncio
async def test_the_decision_on_a_gate_leaves_a_line_to_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Отчёт репетиции считает воронку grep-ом: сколько дошло до ворот и что там нажали."""
    listed(monkeypatch, "12")
    store.stop(12, a_stopped_gate(tmp_path, monkeypatch))

    with caplog.at_level(logging.INFO, logger="app.bot"):
        await on_gate_button(a_press(a_gate_button("прогон", "stop")), NO_CONTEXT)

    assert "gate=stop run=прогон chat=12" in caplog.text


@pytest.mark.asyncio
async def test_start_leaves_a_line_naming_the_run_it_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`/start` не оставлял в логе ничего: по нему нельзя было сказать, чей прогон оборвался."""
    listed(monkeypatch, "12")
    store.stop(12, a_stopped_choice(tmp_path, monkeypatch))

    with caplog.at_level(logging.INFO, logger="app.bot"):
        await on_start(an_update(TextChat("/start")), NO_CONTEXT)

    assert "command=start chat=12 dropped=прогон" in caplog.text


@pytest.mark.asyncio
async def test_start_without_a_stop_says_so_with_a_dash(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, store: FakeStore
) -> None:
    """Пустое место читалось бы как оборванная строка, а прочерк — как ответ."""
    listed(monkeypatch, "12")

    with caplog.at_level(logging.INFO, logger="app.bot"):
        await on_start(an_update(TextChat("/start")), NO_CONTEXT)

    assert "command=start chat=12 dropped=-" in caplog.text


@pytest.mark.asyncio
async def test_a_new_text_run_names_itself_in_the_log_before_it_starts_spending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Первой записью прогона была стадия, а она не знает ни чата, ни номера прогона."""
    listed(monkeypatch, "12")
    monkeypatch.setattr(bot, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(bot, "walk", walk_recording([]))

    with caplog.at_level(logging.INFO, logger="app.bot"):
        await on_text(an_update(TextChat("Идея")), NO_CONTEXT)

    run_id = store.started[0][0]
    assert f"start=text run={run_id} chat=12" in caplog.text


@pytest.mark.asyncio
async def test_a_voice_names_the_stop_it_took_away_from_the_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """На стенде следующий человек говорит голосом: чей выбор при этом пропал — вопрос к логу."""
    listed(monkeypatch, "12")
    store.stop(12, a_stopped_choice(tmp_path, monkeypatch))
    monkeypatch.setattr(bot, "save_voice", saved_empty)
    monkeypatch.setattr(bot, "walk", walk_recording([]))

    with caplog.at_level(logging.INFO, logger="app.bot"):
        await on_voice(an_update(VoiceChat(a_voice(3))), NO_CONTEXT)

    started = store.started[0][0]
    assert f"start=voice run={started} chat=12 dropped=прогон" in caplog.text


@pytest.mark.asyncio
async def test_a_number_outside_the_list_names_the_run_it_was_meant_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Отказ без прогона не сложить с остановкой, которую человек не смог пройти."""
    listed(monkeypatch, "12")
    store.stop(12, a_stopped_choice(tmp_path, monkeypatch))

    with caplog.at_level(logging.INFO, logger="app.bot"):
        await on_text(an_update(TextChat("7")), NO_CONTEXT)

    assert "refusal=unknown_number chat=12 run=прогон" in caplog.text


@pytest.mark.asyncio
async def test_a_button_whose_message_is_gone_still_leaves_a_line(
    caplog: pytest.LogCaptureFixture, store: FakeStore
) -> None:
    """Единственный молчаливый выход обработчика: отвечать некуда, но нажатие было."""
    chat = a_gate_button("прогон", "next")
    press = cast(Update, SimpleNamespace(callback_query=chat, effective_message=None))

    with caplog.at_level(logging.INFO, logger="app.bot"):
        await on_gate_button(press, NO_CONTEXT)

    assert "gate=lost" in caplog.text
    assert chat.answered == 1


@pytest.mark.asyncio
async def test_a_pressed_number_answers_the_choice_the_way_a_typed_one_does(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store: FakeStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Кнопка и набранный номер — один ответ: та же стадия, тот же повтор, та же строка в логе.

    `by=button` в ней и есть ответ на вопрос, ради которого кнопку заводили: у стенда перестали
    набирать текст или всё-таки набирают.
    """
    listed(monkeypatch, "12")
    store.stop(12, a_stopped_choice(tmp_path, monkeypatch))
    seen: list[tuple[str, str, Redo | None]] = []
    monkeypatch.setattr(bot, "walk", walk_recording(seen))
    chat = a_choice_button("прогон", "1")

    with caplog.at_level(logging.INFO, logger="app.bot"):
        await on_choice_button(a_press(chat), NO_CONTEXT)

    assert seen == [
        (
            "прогон",
            "intake",
            Redo(
                kind="choice",
                user_edit="Выбрана идея 1: Бот для онбординга новичков",
                artifact=CANDIDATES,
            ),
        )
    ]
    assert chat.answered == 1
    assert "answer=choice by=button run=прогон chat=12" in caplog.text
    assert store.status["прогон"] == PUBLISHED


@pytest.mark.asyncio
async def test_the_choice_buttons_stay_so_a_broken_redo_can_be_answered_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """Сорвавшийся повтор возвращает остановку: снятая клавиатура отняла бы способ ответить."""
    listed(monkeypatch, "12")
    stopped = a_stopped_choice(tmp_path, monkeypatch)
    store.stop(12, stopped)
    monkeypatch.setattr(bot, "walk", walk_breaking)
    chat = a_choice_button("прогон", "1")

    await on_choice_button(a_press(chat), NO_CONTEXT)

    assert chat.markups == []
    assert chat.edits[-1] == BROKEN_REDO["choice"].format(run_id="прогон")
    assert store.stops[12] == stopped


@pytest.mark.asyncio
async def test_a_choice_button_of_a_run_that_no_longer_waits_moves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    listed(monkeypatch, "12")
    store.stop(12, a_stopped_choice(tmp_path, monkeypatch))
    seen: list[tuple[str, str, Redo | None]] = []
    monkeypatch.setattr(bot, "walk", walk_recording(seen))
    chat = a_choice_button("вчерашний", "1")

    await on_choice_button(a_press(chat), NO_CONTEXT)

    assert chat.replies == [STALE_BUTTON]
    assert seen == []
    assert store.stops[12].run_id == "прогон"


@pytest.mark.asyncio
async def test_a_choice_button_pressed_when_the_run_stands_at_a_gate_moves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """Список с кнопками живёт в чате вечно, а прогон за это время ушёл к воротам.

    Нажатое на нём «1» вернуло бы обход в intake, то есть выбросило бы бриф, которого человек
    ждал: род остановки сверяется до того, как ответ куда-то поедет.
    """
    listed(monkeypatch, "12")
    store.stop(12, a_stopped_gate(tmp_path, monkeypatch))
    seen: list[tuple[str, str, Redo | None]] = []
    monkeypatch.setattr(bot, "walk", walk_recording(seen))
    chat = a_choice_button("прогон", "1")

    await on_choice_button(a_press(chat), NO_CONTEXT)

    assert chat.replies == [STALE_BUTTON]
    assert seen == []
    assert store.stops[12].kind == "gate"


@pytest.mark.asyncio
async def test_a_choice_button_pressed_during_a_run_is_refused_like_a_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store: FakeStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    listed(monkeypatch, "12")
    store.stop(12, a_stopped_choice(tmp_path, monkeypatch))
    chat = a_choice_button("прогон", "1")
    await bot.running.acquire()
    try:
        with caplog.at_level(logging.INFO, logger="app.bot"):
            await on_choice_button(a_press(chat), NO_CONTEXT)
    finally:
        bot.running.release()

    assert chat.replies == [BUSY]
    assert chat.answered == 1
    assert "refusal=busy chat=12 run=прогон" in caplog.text


@pytest.mark.asyncio
async def test_a_choice_button_whose_number_is_not_a_number_is_refused_like_a_stale_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """Данные писал бот, но сообщения переживают выкладку: мусор ушёл бы в стадию правкой."""
    listed(monkeypatch, "12")
    store.stop(12, a_stopped_choice(tmp_path, monkeypatch))
    seen: list[tuple[str, str, Redo | None]] = []
    monkeypatch.setattr(bot, "walk", walk_recording(seen))
    chat = a_choice_button("прогон", "первую")

    await on_choice_button(a_press(chat), NO_CONTEXT)

    assert chat.replies == [STALE_BUTTON]
    assert seen == []


def walk_stopping_in_turn(stops: list[Pause], files: dict[str, str]) -> Walking:
    """Обход, встающий там, где сказано, по одной остановке за вызов.

    Когда остановки кончились, доходит до конца и публикует: лишний вызов обхода обязан
    выглядеть как то, чем он и был бы вживую, — карточки на доске.
    """

    def walking(
        run: Run, start: str, stop: str, on_done: Callable[[Stage], None], redo: Redo | None = None
    ) -> Pause | None:
        (run.root / "outputs").mkdir(parents=True, exist_ok=True)
        for path, content in files.items():
            (run.root / path).write_text(content, encoding="utf-8")
        if stops:
            return stops.pop(0)
        (run.root / "outputs/publish.json").write_text('{"I-001": {}}', encoding="utf-8")
        return None

    return walking


@pytest.mark.asyncio
async def test_a_button_of_the_gate_the_run_has_already_left_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """«Править» кнопок не снимает, поэтому у прогона остаётся живое сообщение прошлых ворот.

    Нажатое на нём «Дальше» уводило обход со стадии, где прогон стоит сейчас: после ворот
    decompose оно означало `after("decompose")`, то есть публикацию бэклога, которого никто не
    подтверждал. Решение принимают на конкретных воротах, а не «где-то в этом прогоне».
    """
    listed(monkeypatch, "12")
    store.stop(12, a_stopped_gate(tmp_path, monkeypatch))
    artifacts = {BRIEF: REAL_BRIEF, ISSUES_JSON: REAL_ISSUES, ISSUES_MD: "# Backlog\n"}
    at_brief = Pause(stage="brief", artifact=BRIEF, kind="gate")
    at_decompose = Pause(stage="decompose", artifact=ISSUES_MD, kind="gate")
    monkeypatch.setattr(bot, "walk", walk_stopping_in_turn([at_brief, at_decompose], artifacts))

    await on_gate_button(a_press(a_gate_button("прогон", "edit")), NO_CONTEXT)
    await on_text(an_update(TextChat("Убери пятый раздел")), NO_CONTEXT)
    await on_gate_button(a_press(a_gate_button("прогон", "next")), NO_CONTEXT)

    assert store.stops[12].stage == "decompose"

    stale = a_gate_button("прогон", "next", stage="brief")
    await on_gate_button(a_press(stale), NO_CONTEXT)

    assert stale.replies == [STALE_BUTTON]
    assert store.stops[12].stage == "decompose"
    assert store.status["прогон"] == AWAITING_GATE


@pytest.mark.asyncio
async def test_the_button_carries_the_gate_it_grew_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    listed(monkeypatch, "12")
    monkeypatch.setattr(bot, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(bot, "walk", walk_stopping_at_a_gate("brief", BRIEF, {BRIEF: REAL_BRIEF}))
    chat = TextChat("Идея")

    await on_text(an_update(chat), NO_CONTEXT)

    keyboard = chat.keyboards[-1]
    assert isinstance(keyboard, InlineKeyboardMarkup)
    run_id = store.started[0][0]
    assert [button.callback_data for button in keyboard.inline_keyboard[0]] == [
        f"gate:{run_id}:brief:next",
        f"gate:{run_id}:brief:edit",
        f"gate:{run_id}:brief:stop",
    ]


@pytest.mark.asyncio
async def test_a_gate_whose_file_did_not_go_still_shows_its_buttons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """Строка уже в `awaiting_gate`: без кнопок человек остаётся с галочками и без выхода."""
    listed(monkeypatch, "12")
    monkeypatch.setattr(bot, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(bot, "walk", walk_stopping_at_a_gate("brief", BRIEF, {BRIEF: REAL_BRIEF}))
    chat = TextChat("Идея")
    chat.document_fails = True

    await on_text(an_update(chat), NO_CONTEXT)

    assert chat.edits[-1].startswith("Бриф готов")
    assert isinstance(chat.keyboards[-1], InlineKeyboardMarkup)
    assert chat.documents == []


@pytest.mark.asyncio
async def test_start_from_a_stranger_gets_silence_like_every_other_message(
    monkeypatch: pytest.MonkeyPatch, store: FakeStore, caplog: pytest.LogCaptureFixture
) -> None:
    """`/start` был единственным обработчиком без белого списка: чужой чат получал приветствие."""
    listed(monkeypatch, "-100500")
    chat = TextChat("/start")

    with caplog.at_level(logging.WARNING, logger="app.bot"):
        await on_start(an_update(chat), NO_CONTEXT)

    assert chat.replies == []
    assert "refusal=stranger chat=12" in caplog.text


def test_a_gate_that_no_digest_knows_about_falls_instead_of_lying(tmp_path: Path) -> None:
    """Ворота объявляет список стадий, содержание пишет код: третьи ворота обязаны упереться."""
    run = Run(root=tmp_path, run_id="прогон", lang="ru")

    with pytest.raises(ValueError, match="у ворот после prd"):
        bot.gate_text(run, Pause(stage="prd", artifact=BRIEF, kind="gate"))


@pytest.mark.asyncio
async def test_a_continued_run_stops_at_the_next_gate_and_shows_its_own_voice_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """План обещал обе проверки, а тест «Дальше» шёл через двойник, который не встаёт нигде.

    Продолженный прогон обязан упереться в следующие ворота (иначе `auto_approve` из строки
    ничего не значит) и остаться голосовым в подписи прогресса: записи на диске уже нет.
    """
    listed(monkeypatch, "12")
    store.stop(12, a_stopped_gate(tmp_path, monkeypatch, source="voice"))
    at_decompose = Pause(stage="decompose", artifact=ISSUES_MD, kind="gate")
    monkeypatch.setattr(
        bot,
        "walk",
        walk_stopping_in_turn(
            [at_decompose], {ISSUES_JSON: REAL_ISSUES, ISSUES_MD: "# Backlog\n"}
        ),
    )
    chat = a_gate_button("прогон", "next")

    await on_gate_button(a_press(chat), NO_CONTEXT)

    assert chat.replies[0].splitlines()[2] == f"✓ {VOICE_INGEST_LABEL}"
    assert chat.edits[-1].startswith("Бэклог готов")
    assert store.stops[12].stage == "decompose"
    assert store.status["прогон"] == AWAITING_GATE


@pytest.mark.asyncio
async def test_a_button_from_before_the_format_changed_is_refused_like_a_stale_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """Сообщения переживают выкладку: кнопка прошлой формы роняла обработчик молчанием."""
    listed(monkeypatch, "12")
    store.stop(12, a_stopped_gate(tmp_path, monkeypatch))
    chat = a_gate_button("прогон", "next")
    chat.data = "gate:прогон:next"

    await on_gate_button(a_press(chat), NO_CONTEXT)

    assert chat.replies == [STALE_BUTTON]
    assert store.stops[12].stage == "brief"


@pytest.fixture(autouse=True)
def forgotten_gates() -> Iterator[None]:
    """Режим чата живёт в памяти процесса, а тесты идут в одном: чужой выбор не наследуется."""
    yield
    bot.AUTO_APPROVE_BY_CHAT.clear()


@pytest.mark.asyncio
async def test_gates_on_makes_the_next_run_stop_and_gates_off_makes_it_run_through(
    monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """Ведущий переключает ворота, не гася бота: это второй акт показа."""
    listed(monkeypatch, "12")
    monkeypatch.setattr(settings, "auto_approve", True)

    await on_gates(an_update(TextChat("/gates on")), NO_CONTEXT)

    assert not started_run("прогон", 12).auto_approve

    await on_gates(an_update(TextChat("/gates off")), NO_CONTEXT)

    assert started_run("прогон", 12).auto_approve


@pytest.mark.asyncio
async def test_gates_without_a_word_says_how_it_is_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    listed(monkeypatch, "12")
    chat = TextChat("/gates")

    await on_gates(an_update(chat), NO_CONTEXT)

    assert chat.replies == [GATES_STATE[False]]
    assert bot.AUTO_APPROVE_BY_CHAT == {}


@pytest.mark.asyncio
async def test_gates_with_a_word_it_does_not_know_keeps_the_mode(
    monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    listed(monkeypatch, "12")
    await on_gates(an_update(TextChat("/gates on")), NO_CONTEXT)
    chat = TextChat("/gates maybe")

    await on_gates(an_update(chat), NO_CONTEXT)

    assert chat.replies == [GATES_UNKNOWN]
    assert not auto_approve_for(12)


@pytest.mark.asyncio
async def test_the_answer_says_the_switch_takes_the_next_run_not_this_one(
    monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """Режим идущего прогона записан в его строке, и менять его на ходу значило бы соврать."""
    listed(monkeypatch, "12")
    chat = TextChat("/gates on")

    await on_gates(an_update(chat), NO_CONTEXT)

    assert chat.replies == [GATES_STATE[False] + GATES_FROM_NEXT_RUN]


def test_a_chat_that_never_asked_takes_the_mode_from_the_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "auto_approve", True)

    assert auto_approve_for(777)


@pytest.mark.asyncio
async def test_one_chat_switching_gates_leaves_the_other_alone(
    monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """Белый список держит несколько чатов: переключение из одного — не решение за другой."""
    listed(monkeypatch, "12, 13")
    monkeypatch.setattr(settings, "auto_approve", True)

    await on_gates(an_update(TextChat("/gates on")), NO_CONTEXT)

    assert not auto_approve_for(12)
    assert auto_approve_for(13)


@pytest.mark.asyncio
async def test_gates_from_a_stranger_gets_silence(
    monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    listed(monkeypatch, "-100500")
    chat = TextChat("/gates off")

    await on_gates(an_update(chat), NO_CONTEXT)

    assert chat.replies == []
    assert bot.AUTO_APPROVE_BY_CHAT == {}


@pytest.mark.asyncio
async def test_switching_gates_leaves_a_line_to_count(
    monkeypatch: pytest.MonkeyPatch, store: FakeStore, caplog: pytest.LogCaptureFixture
) -> None:
    listed(monkeypatch, "12")

    with caplog.at_level(logging.INFO, logger="app.bot"):
        await on_gates(an_update(TextChat("/gates on")), NO_CONTEXT)

    assert "command=gates chat=12 gates=on" in caplog.text


@pytest.mark.asyncio
async def test_a_stopped_run_keeps_its_own_gates_after_the_chat_switched_them_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """Режим прогона записан в его строке: команда меняет умолчание чата, а не прогон."""
    listed(monkeypatch, "12")
    stopped = a_stopped_gate(tmp_path, monkeypatch, auto_approve=False)

    await on_gates(an_update(TextChat("/gates off")), NO_CONTEXT)

    assert not bot.continued(stopped).auto_approve
