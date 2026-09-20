"""Цена и вопрос о согласии: один текст на команду с ноутбука и на папку входящих."""

from pathlib import Path

import pytest

from app.config import settings
from app.meeting import (
    CONSENT_QUESTION,
    consent_question,
    copied_recording,
    one_pass_minutes,
    price_line,
)

AN_HOUR = 3600


def test_price_line_names_whisper_exactly() -> None:
    assert "$0.36" in price_line(AN_HOUR)


def test_price_line_names_the_measured_review_cost() -> None:
    """Оценка стоит на одном живом прогоне, и вопрос называет его, а не выдаёт число за истину."""
    line = price_line(AN_HOUR)

    assert "44 минуты" in line
    assert "$0.37" in line


def test_price_line_says_around_and_never_a_bare_number() -> None:
    assert "около" in price_line(AN_HOUR)


def test_price_line_scales_the_review_estimate_with_length() -> None:
    assert "$0.08" in price_line(600)
    assert "$0.50" in price_line(AN_HOUR)


def test_price_line_warns_when_the_review_may_not_fit_one_pass() -> None:
    """Расшифровка к моменту обрыва уже оплачена, и молчать об этом до согласия нельзя."""
    assert "ANTHROPIC_MAX_TOKENS" in price_line(60 * 60)
    assert "ANTHROPIC_MAX_TOKENS" not in price_line(40 * 60)


def test_the_one_pass_warning_moves_with_anthropic_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Настройку поднимают ровно затем, чтобы длинная запись прошла: граница едет вместе с ней."""
    assert one_pass_minutes() == 52

    monkeypatch.setattr(settings, "anthropic_max_tokens", 48000)

    assert one_pass_minutes() == 103
    assert "ANTHROPIC_MAX_TOKENS" not in price_line(60 * 60)


def test_the_consent_question_names_the_file_the_length_and_the_price() -> None:
    asked = consent_question("созвон.m4a", AN_HOUR)

    assert asked.startswith("Запись: созвон.m4a, 60 минут.")
    assert asked.endswith(CONSENT_QUESTION)
    assert price_line(AN_HOUR) in asked


def test_the_recording_is_copied_and_the_original_is_left_alone(tmp_path: Path) -> None:
    recording = tmp_path / "созвон.m4a"
    recording.write_bytes(b"m4a")

    copy = copied_recording(recording, tmp_path / "runs" / "abc")

    assert copy.name == "recording.m4a"
    assert copy.read_bytes() == b"m4a"
    assert recording.exists()
