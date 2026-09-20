"""Обход папки входящих и её журнал. Ни один тест не ждёт интервала и не трогает сеть."""

from pathlib import Path

import pytest

from app import inbox
from app.inbox import (
    DONE,
    Journal,
    Observation,
    Ready,
    fingerprint,
    moved_to_done,
    observed_now,
    ready_recordings,
)
from app.transcribe import TranscriptionError

TEN_MINUTES = 600


@pytest.fixture(autouse=True)
def readable_recordings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Шов третьего сита: длительность называет ffprobe, и в тестах его не зовут."""
    monkeypatch.setattr(inbox, "recording_seconds", lambda path: TEN_MINUTES)


def a_recording(folder: Path, name: str = "REC001.m4a", body: bytes = b"m4a") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    recording = folder / name
    recording.write_bytes(body)
    return recording


def swept(
    folder: Path, seen: dict[str, Observation], known: set[str] | None = None
) -> tuple[list[Ready], dict[str, Observation]]:
    return ready_recordings(folder, seen, known or set())


def test_inbox_waits_for_a_file_that_is_still_growing(tmp_path: Path) -> None:
    recording = a_recording(tmp_path)
    _, first = swept(tmp_path, {})
    recording.write_bytes("m4a ещё немного".encode())

    ready, _ = swept(tmp_path, first)

    assert ready == []


def test_inbox_offers_a_file_that_stopped_growing(tmp_path: Path) -> None:
    """Два одинаковых наблюдения подряд — признак того, что копирование кончилось."""
    a_recording(tmp_path)
    ready, first = swept(tmp_path, {})
    assert ready == []

    ready, _ = swept(tmp_path, first)

    [found] = ready
    assert found.path.name == "REC001.m4a"
    assert found.seconds == TEN_MINUTES


def test_inbox_ignores_hidden_and_half_copied_files(tmp_path: Path) -> None:
    for name in (".DS_Store", "._REC001.m4a", "REC001.m4a.part", "REC002.m4a.crdownload"):
        a_recording(tmp_path, name)

    ready, first = swept(tmp_path, {})
    ready, _ = swept(tmp_path, first)

    assert ready == []


def test_inbox_ignores_the_done_folder(tmp_path: Path) -> None:
    a_recording(tmp_path / DONE, "REC001.m4a")

    ready, first = swept(tmp_path, {})
    ready, _ = swept(tmp_path, first)

    assert ready == []


def test_inbox_ignores_a_file_that_is_not_a_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Имя и расширение ничего не значат: запись ли это, говорит ffprobe."""

    def unreadable(path: Path) -> int:
        raise TranscriptionError(f"ffprobe не смог прочитать {path.name}")

    monkeypatch.setattr(inbox, "recording_seconds", unreadable)
    a_recording(tmp_path, "договор.pdf")

    ready, first = swept(tmp_path, {})
    ready, _ = swept(tmp_path, first)

    [found] = ready
    assert found.seconds is None


def test_inbox_does_not_offer_a_file_the_journal_already_decided(tmp_path: Path) -> None:
    recording = a_recording(tmp_path)
    ready, first = swept(tmp_path, {})
    known = {fingerprint(recording, observed_now(recording))}

    ready, _ = swept(tmp_path, first, known)

    assert ready == []


# --- Журнал runs/inbox.json ---


def a_ready(recording: Path) -> Ready:
    observed = observed_now(recording)
    return Ready(
        path=recording,
        fingerprint=fingerprint(recording, observed),
        observed=observed,
        seconds=TEN_MINUTES,
    )


def test_journal_remembers_a_refusal_across_a_restart(tmp_path: Path) -> None:
    recording = a_recording(tmp_path / "inbox")
    journal = Journal(tmp_path / "runs" / "inbox.json")
    journal.decided(a_ready(recording), "no")

    _, first = swept(recording.parent, {})
    ready, _ = swept(recording.parent, first, Journal(tmp_path / "runs" / "inbox.json").closed())

    assert ready == []


def test_journal_asks_again_about_a_new_recording_with_a_reused_name(tmp_path: Path) -> None:
    """Диктофон пишет REC001.m4a по кругу: по одному имени встреча пропала бы молча."""
    folder = tmp_path / "inbox"
    recording = a_recording(folder)
    journal = Journal(tmp_path / "runs" / "inbox.json")
    journal.decided(a_ready(recording), "no")
    recording.write_bytes("совсем другая запись".encode())

    _, first = swept(folder, {})
    ready, _ = swept(folder, first, journal.closed())

    assert [found.path.name for found in ready] == ["REC001.m4a"]


def test_journal_starts_empty_when_the_file_is_broken(tmp_path: Path) -> None:
    """Битый журнал — переспросить про лежащее, а не упасть при старте бота."""
    path = tmp_path / "inbox.json"
    path.write_text("{половина", encoding="utf-8")

    assert Journal(path).closed() == set()


def test_journal_survives_a_write_that_did_not_finish(tmp_path: Path) -> None:
    recording = a_recording(tmp_path / "inbox")
    path = tmp_path / "runs" / "inbox.json"
    journal = Journal(path)
    journal.decided(a_ready(recording), "yes", run_id="abc123")
    path.with_name("inbox.json.tmp").write_text("{половина", encoding="utf-8")

    reread = Journal(path)

    assert reread.closed() == journal.closed()
    assert reread.entries[a_ready(recording).fingerprint].run_id == "abc123"


def test_journal_keeps_offering_a_file_it_only_postponed(tmp_path: Path) -> None:
    """Отложенный вопрос — память об уведомлении, а не решение: спросить всё ещё надо."""
    folder = tmp_path / "inbox"
    recording = a_recording(folder)
    journal = Journal(tmp_path / "runs" / "inbox.json")
    ready = a_ready(recording)
    journal.decided(ready, "postponed")

    _, first = swept(folder, {})
    offered, _ = swept(folder, first, journal.closed())

    assert [found.fingerprint for found in offered] == [ready.fingerprint]
    assert journal.told_about(ready.fingerprint)


# --- Перенос в done/ ---


def test_done_folder_keeps_a_file_that_was_already_there(tmp_path: Path) -> None:
    """Имена диктофона идут по кругу, и замена лежащего файла — это удаление исходника."""
    a_recording(tmp_path / DONE, body="прошлая встреча".encode())
    recording = a_recording(tmp_path, body="сегодняшняя встреча".encode())

    moved = moved_to_done(recording)

    assert not recording.exists()
    assert (tmp_path / DONE / "REC001.m4a").read_bytes() == "прошлая встреча".encode()
    assert moved.name == "REC001 (2).m4a"
    assert moved.read_bytes() == "сегодняшняя встреча".encode()


def test_done_folder_counts_up_until_the_name_is_free(tmp_path: Path) -> None:
    a_recording(tmp_path / DONE)
    a_recording(tmp_path / DONE, "REC001 (2).m4a")
    recording = a_recording(tmp_path)

    assert moved_to_done(recording).name == "REC001 (3).m4a"
