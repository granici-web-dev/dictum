import asyncio
import json
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from telegram import Message, Update, Voice
from telegram.ext import Application, ContextTypes

from app import bot
from app.bot import (
    BROKEN_REDO,
    BUSY,
    EMPTY,
    FIRST_STAGE,
    GREETING,
    HEARD,
    LABEL,
    MAX_VOICE_SECONDS,
    ASK_AGAIN,
    NOTHING_HEARD,
    PICK_ONE,
    VOICE_INGEST_LABEL,
    Waiting,
    allowed_chats,
    cards_published,
    chosen_edit,
    demo_run,
    finished_text,
    follow,
    main,
    on_start,
    on_text,
    on_voice,
    outcome,
    progress_text,
    refuse,
    too_long,
)
from app.candidates import parse_candidates
from app.config import ConfigError, MissingApiKey, settings
from app.pipeline import CANDIDATES, NAMES, Stage, stages_between
from app.run import Pause, Redo, Run, walk
from tests.test_candidates import MULTIPLE, NONE, NONE_EMPTY


@pytest.fixture(autouse=True)
def no_stopped_runs() -> Iterator[None]:
    """Остановки живут в модуле, и чужая, забытая в словаре, свернула бы следующий тест."""
    bot.paused.clear()
    yield
    bot.paused.clear()


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
    text = progress_text(a_run(), [])

    printed = [line[2:] for line in text.splitlines()[2:]]
    assert printed == [LABEL[name] for name in NAMES]


def test_the_progress_marks_what_is_done_and_what_runs_now() -> None:
    text = progress_text(a_run(), ["ingest", "intake"])

    marks = [line[0] for line in text.splitlines()[2:]]
    assert marks == ["✓", "✓", "▸", "·", "·", "·", "·"]


def test_a_finished_walk_marks_everything_and_points_at_nothing() -> None:
    text = progress_text(a_run(), list(NAMES))

    assert [line[0] for line in text.splitlines()[2:]] == ["✓"] * len(NAMES)


def test_the_progress_carries_the_run_id_so_the_log_can_be_found() -> None:
    assert progress_text(a_run("a1b2c3d4"), []).startswith("Прогон a1b2c3d4")


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
    printed = progress_text(a_run(audio=Path("voice.oga")), []).splitlines()[2:]

    assert printed[0] == f"▸ {VOICE_INGEST_LABEL}"
    assert printed[1:] == [f"· {LABEL[name]}" for name in NAMES[1:]]


def ready_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Всё, без чего бот не поднимется. Каждый тест старта ломает ровно одно."""
    monkeypatch.setattr(settings, "telegram_bot_token", "1:token")
    monkeypatch.setattr(settings, "allow_live_api", True)
    monkeypatch.setattr(settings, "openai_api_key", "test")
    listed(monkeypatch, "12")
    monkeypatch.setattr(bot, "ffmpeg_installed", lambda: True)


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

    async def edit_text(self, text: str) -> Message:
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
        datetime.now(timezone.utc),
    )

    assert note.edits[-1] == finished_text(tmp_path)
    assert len(note.edits) == len(NAMES) + 1


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
async def test_a_missing_candidates_file_still_ends_the_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Чтение артефакта идёт после обхода: без перехвата человек остался бы без концовки."""
    monkeypatch.setattr(bot, "walk", walk_stopping_on_choice)
    run = Run(root=tmp_path, run_id="прогон", lang="ru", text="…", auto_approve=True)

    said = (await outcome(run, lambda stage: None, FIRST_STAGE, None)).text

    assert "сорвался" in said


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

    async def edit_text(self, text: str) -> Message:
        self.edits.append(text)
        return cast(Message, self)


class VoiceChat(TextChat):
    def __init__(self, voice: Voice) -> None:
        super().__init__("")
        self.voice = voice


def an_update(message: object) -> Update:
    return cast(Update, SimpleNamespace(message=message))


NO_CONTEXT = cast(ContextTypes.DEFAULT_TYPE, None)


def a_stopped_choice(root: Path, candidates: str = MULTIPLE) -> Waiting:
    """Остановка без файла на диске: список она несёт с собой, читать его заново некому."""
    run = Run(root=root, run_id="прогон", lang="ru", auto_approve=True)
    return Waiting(
        run=run, stage="intake", artifact=CANDIDATES, found=parse_candidates(candidates)
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Расшифровка уже есть: новый прогон означал бы просьбу надиктовать идею заново."""
    listed(monkeypatch, "12")
    bot.paused[12] = a_stopped_choice(tmp_path)
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
    assert 12 not in bot.paused
    assert chat.replies[0].splitlines()[2] == "✓ принял идею"


@pytest.mark.asyncio
async def test_a_number_outside_the_list_keeps_the_run_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Прогон не сбрасывается из-за опечатки: человек ещё выбирает."""
    listed(monkeypatch, "12")
    stopped = a_stopped_choice(tmp_path)
    bot.paused[12] = stopped
    seen: list[tuple[str, str, Redo | None]] = []
    monkeypatch.setattr(bot, "walk", walk_recording(seen))
    chat = TextChat("7")

    await on_text(an_update(chat), NO_CONTEXT)

    assert chat.replies == ["Идей всего 2. Пришлите номер от 1 до 2 или саму идею словами."]
    assert bot.paused[12] == stopped
    assert seen == []


@pytest.mark.asyncio
async def test_the_list_lives_in_the_stop_so_a_lost_file_cannot_swallow_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ответ разбирался повторным чтением candidates.md, и стёртый файл ронял обработчик молча."""
    listed(monkeypatch, "12")
    bot.paused[12] = a_stopped_choice(tmp_path)
    seen: list[tuple[str, str, Redo | None]] = []
    monkeypatch.setattr(bot, "walk", walk_recording(seen))

    await on_text(an_update(TextChat("2")), NO_CONTEXT)

    assert not (tmp_path / CANDIDATES).exists()
    assert [start for _, start, _ in seen] == ["intake"]


@pytest.mark.asyncio
async def test_a_digit_that_is_no_number_goes_to_the_stage_instead_of_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """У «²» isdigit истинен, а int падает: обработчик срывался, и человек не получал ничего."""
    listed(monkeypatch, "12")
    bot.paused[12] = a_stopped_choice(tmp_path)
    seen: list[tuple[str, str, Redo | None]] = []
    monkeypatch.setattr(bot, "walk", walk_recording(seen))

    await on_text(an_update(TextChat("²")), NO_CONTEXT)

    assert [redo.user_edit for _, _, redo in seen if redo] == ["²"]


@pytest.mark.asyncio
async def test_an_empty_message_is_refused_and_the_stop_stays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пробел снимал остановку и уходил в стадию правкой без единого слова."""
    listed(monkeypatch, "12")
    stopped = a_stopped_choice(tmp_path)
    bot.paused[12] = stopped
    seen: list[tuple[str, str, Redo | None]] = []
    monkeypatch.setattr(bot, "walk", walk_recording(seen))
    chat = TextChat("   ")

    await on_text(an_update(chat), NO_CONTEXT)

    assert chat.replies == [EMPTY]
    assert bot.paused[12] == stopped
    assert seen == []


@pytest.mark.asyncio
async def test_a_broken_redo_keeps_the_recording_and_the_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Иначе сорванная стадия теряет расшифровку — та самая потеря, ради которой был P3-04."""
    listed(monkeypatch, "12")
    stopped = a_stopped_choice(tmp_path)
    bot.paused[12] = stopped

    monkeypatch.setattr(bot, "walk", walk_breaking)
    chat = TextChat("2")

    await on_text(an_update(chat), NO_CONTEXT)

    assert chat.edits[-1] == BROKEN_REDO.format(run_id="прогон")
    assert bot.paused[12] == stopped


@pytest.mark.asyncio
async def test_start_waits_for_the_run_so_its_reset_is_not_written_over(tmp_path: Path) -> None:
    """`/start` во время прогона снимал пустоту: остановку прогон записывал уже после него."""
    bot.paused[12] = a_stopped_choice(tmp_path)
    chat = TextChat("/start")
    await bot.running.acquire()
    started = asyncio.create_task(on_start(an_update(chat), NO_CONTEXT))
    await asyncio.sleep(0.01)
    waited = chat.replies == [] and 12 in bot.paused
    bot.running.release()
    await started

    assert waited
    assert 12 not in bot.paused
    assert chat.replies == [GREETING]


async def saved_empty(voice: Voice, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"")


@pytest.mark.asyncio
async def test_a_voice_always_starts_a_new_run_and_forgets_the_stopped_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """На стенде следующий человек говорит голосом: его запись — не правка к чужому выбору."""
    listed(monkeypatch, "12")
    bot.paused[12] = a_stopped_choice(tmp_path)
    monkeypatch.setattr(bot, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(bot, "save_voice", saved_empty)
    seen: list[tuple[str, str, Redo | None]] = []
    monkeypatch.setattr(bot, "walk", walk_recording(seen))

    await on_voice(an_update(VoiceChat(a_voice(3))), NO_CONTEXT)

    assert 12 not in bot.paused
    assert [(start, redo) for _, start, redo in seen] == [("ingest", None)]


@pytest.mark.asyncio
async def test_a_choice_after_a_voice_is_remembered_as_after_a_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Живой прогон 664534620c9a1c10: остановку записывал только on_text, а голос её терял.

    На стенде идею наговаривают, поэтому «1» после голосового уходило новым прогоном —
    из одного слова, на чужом языке и без идеи внутри.
    """
    listed(monkeypatch, "12")
    monkeypatch.setattr(bot, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(bot, "save_voice", saved_empty)
    monkeypatch.setattr(bot, "walk", walk_stopping_with(MULTIPLE))

    await on_voice(an_update(VoiceChat(a_voice(3))), NO_CONTEXT)

    assert bot.paused[12].stage == "intake"
    assert bot.paused[12].artifact == CANDIDATES


@pytest.mark.asyncio
async def test_start_drops_a_stopped_run_so_a_new_idea_can_begin(tmp_path: Path) -> None:
    """Выхода из остановки больше нет: текст — это выбор, а голосовое есть не у всех."""
    bot.paused[12] = a_stopped_choice(tmp_path)
    chat = TextChat("/start")

    await on_start(an_update(chat), NO_CONTEXT)

    assert 12 not in bot.paused
    assert chat.replies == [GREETING]


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed(monkeypatch, "12")
    monkeypatch.setattr(bot, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(bot, "walk", walk_stopping_with(MULTIPLE))
    chat = TextChat("Две идеи разом")

    await on_text(an_update(chat), NO_CONTEXT)

    assert bot.paused[12].stage == "intake"
    assert bot.paused[12].artifact == CANDIDATES
    assert chat.edits[-1].startswith(HEARD)


@pytest.mark.asyncio
async def test_a_recording_with_nothing_in_it_is_not_worth_waiting_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ждать ответа не на что: в записи не было ничего, и правка пришлась бы к пустому."""
    listed(monkeypatch, "12")
    monkeypatch.setattr(bot, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(bot, "walk", walk_stopping_with(NONE_EMPTY))
    chat = TextChat("Эээ")

    await on_text(an_update(chat), NO_CONTEXT)

    assert bot.paused == {}
    assert chat.edits[-1].startswith(NOTHING_HEARD)
