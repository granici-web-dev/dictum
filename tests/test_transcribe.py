import subprocess
from pathlib import Path

import pytest

from app import transcribe
from app.config import LiveApiNotAllowed, settings
from app.transcribe import NOTHING_HEARD, TranscriptionError, convert_to_mp3
from tests.helpers import InstallResponses, heard


@pytest.fixture
def recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Голосовое на диске и перекодировка вместо ffmpeg: тест не зависит от бинарника в системе."""
    voice = tmp_path / "voice.oga"
    voice.write_bytes(b"ogg")

    def convert(source: Path) -> Path:
        target = source.with_suffix(".mp3")
        target.write_bytes(b"mp3")
        return target

    monkeypatch.setattr(transcribe, "convert_to_mp3", convert)
    return voice


def test_the_transcript_takes_the_language_whisper_detected(
    whisper: InstallResponses, recording: Path
) -> None:
    whisper([heard("Хочу бота, который напоминает о дедлайнах.", "russian", 12.4)])

    heard_out = transcribe.transcribe(recording)

    assert heard_out.text == "Хочу бота, который напоминает о дедлайнах."
    assert heard_out.lang == "ru"
    assert heard_out.duration_seconds == 12


def test_the_call_asks_for_the_format_that_carries_the_language(
    whisper: InstallResponses, recording: Path
) -> None:
    requests = whisper([heard("Идея.")])

    transcribe.transcribe(recording)

    sent = requests[0].content
    assert b"whisper-1" in sent
    assert b"verbose_json" in sent


def test_a_language_outside_the_table_keeps_the_name_whisper_returned(
    whisper: InstallResponses, recording: Path
) -> None:
    """Соврать «de» вместо нераспознанного языка нельзя: артефакты прогона несут его дальше."""
    whisper([heard("Nataka bot.", "swahili")])

    assert transcribe.transcribe(recording).lang == "swahili"


def test_the_recording_is_gone_once_the_text_is_in_hand(
    whisper: InstallResponses, recording: Path
) -> None:
    whisper([heard("Идея.")])

    transcribe.transcribe(recording)

    assert not recording.exists()
    assert not recording.with_suffix(".mp3").exists()


def test_keep_audio_leaves_both_files_on_disk(
    whisper: InstallResponses, recording: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "keep_audio", True)
    whisper([heard("Идея.")])

    transcribe.transcribe(recording)

    assert recording.exists()
    assert recording.with_suffix(".mp3").exists()


def test_silence_is_refused_before_a_single_stage_is_paid_for(
    whisper: InstallResponses, recording: Path
) -> None:
    whisper([heard("   ")])

    with pytest.raises(TranscriptionError, match=NOTHING_HEARD):
        transcribe.transcribe(recording)


def test_transcription_without_the_live_flag_does_not_even_start_ffmpeg(
    recording: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Флаг проверяется до подпроцесса: иначе прогон жжёт процессор ради отказа."""

    def never(source: Path) -> Path:
        raise AssertionError("ffmpeg запущен до проверки ALLOW_LIVE_API")

    monkeypatch.setattr(transcribe, "convert_to_mp3", never)

    with pytest.raises(LiveApiNotAllowed, match="ALLOW_LIVE_API"):
        transcribe.transcribe(recording)


def test_a_missing_ffmpeg_says_how_to_install_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subprocess, "run", missing_binary)

    with pytest.raises(TranscriptionError, match="brew install ffmpeg"):
        convert_to_mp3(tmp_path / "voice.oga")


def test_ffmpeg_that_could_not_read_the_file_names_it_and_quotes_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fails(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(
            1, "ffmpeg", stderr="ffmpeg version 7.1\nvoice.oga: Invalid data found\n"
        )

    monkeypatch.setattr(subprocess, "run", fails)

    with pytest.raises(TranscriptionError, match="Invalid data found") as refusal:
        convert_to_mp3(tmp_path / "voice.oga")
    assert "voice.oga" in str(refusal.value)


def missing_binary(*args: object, **kwargs: object) -> None:
    raise FileNotFoundError(2, "No such file or directory: 'ffmpeg'")


def test_the_recording_is_recoded_the_way_whisper_is_fed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mp3 16 kHz mono — контракт SPEC §3.1, и ошибка в -ar или -ac уехала бы зелёной."""
    asked: list[list[str]] = []

    def remember(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        asked.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", remember)

    target = convert_to_mp3(tmp_path / "voice.oga")

    assert target.name == "voice.mp3"
    assert asked[0][:2] == ["ffmpeg", "-y"]
    assert asked[0][-5:] == ["-ac", "1", "-ar", "16000", str(target)]
