"""Цена и вопрос о согласии: один текст на команду с ноутбука и на папку входящих."""

from pathlib import Path

import pytest

from app.config import settings
from app.meeting import (
    CONSENT_QUESTION,
    consent_question,
    copied_recording,
    longer_than_one_whisper_request,
    one_pass_minutes,
    price_line,
    review_price,
    too_long_refusal,
)

AN_HOUR = 3600


def dollars(price: str) -> float:
    return float(price.removeprefix("$"))


def test_price_line_names_whisper_exactly() -> None:
    assert "$0.36" in price_line(AN_HOUR)


def test_price_line_names_both_measured_review_costs() -> None:
    """Оценка стоит на двух живых прогонах, и вопрос называет оба, а не выдаёт число за истину."""
    line = price_line(AN_HOUR)

    assert "4 минуты" in line
    assert "$0.10" in line
    assert "44 минуты" in line
    assert "$0.25" in line


def test_price_line_says_around_and_never_a_bare_number() -> None:
    assert "около" in price_line(AN_HOUR)


def test_the_review_price_follows_the_sonnet_5_rate() -> None:
    """Прямая через две точки обязана проходить через сами точки, иначе она не про них.

    Точки пересчитаны по действующему тарифу $2 / $10: по $3 / $15 считался claude-sonnet-4-6,
    и цена перед оплатой называла числа чужой модели.
    """
    assert dollars(review_price(3 * 60)) == pytest.approx(0.10, abs=0.02)
    assert dollars(review_price(44 * 60)) == pytest.approx(0.25, abs=0.02)
    assert review_price(AN_HOUR) == "$0.31"


def test_the_review_estimate_still_grows_with_length() -> None:
    """Постоянная часть не съедает наклон: час записи дороже сорока четырёх минут."""
    assert dollars(review_price(AN_HOUR)) > dollars(review_price(44 * 60))


def test_a_two_hour_recording_gets_no_one_pass_warning() -> None:
    """Предел потребности владельца — два часа, и при умолчании разбор их берёт одним проходом."""
    assert "ANTHROPIC_MAX_TOKENS" not in price_line(120 * 60)


def test_the_one_pass_warning_still_fires_on_a_lowered_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Строка живая, а не мёртвая: её мёртвость при умолчании — цель работы, а не лишний код."""
    monkeypatch.setattr(settings, "anthropic_max_tokens", 12000)

    assert "ANTHROPIC_MAX_TOKENS" in price_line(60 * 60)


def test_the_one_pass_warning_moves_with_anthropic_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Настройку поднимают ровно затем, чтобы длинная запись прошла: граница едет вместе с ней.

    При умолчании 64 000 граница садится на границу одного запроса Whisper: всё, что длиннее,
    отвергается раньше, чем дойдёт до этой строки.
    """
    assert one_pass_minutes() == 138

    monkeypatch.setattr(settings, "anthropic_max_tokens", 24000)

    assert one_pass_minutes() == 52
    assert "ANTHROPIC_MAX_TOKENS" in price_line(60 * 60)


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


def test_a_recording_longer_than_one_whisper_request_is_refused() -> None:
    """Граница фазы 1 — один запрос Whisper: дальше нужна нарезка, а не чужая ошибка по размеру."""
    assert longer_than_one_whisper_request(139 * 60)
    assert not longer_than_one_whisper_request(137 * 60)


def test_the_refusal_by_length_names_the_file_and_the_limit() -> None:
    refusal = too_long_refusal("созвон.m4a", 180 * 60)

    assert "созвон.m4a" in refusal
    assert "180 минут" in refusal
    assert "138" in refusal
