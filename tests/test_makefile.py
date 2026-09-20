"""Цели Makefile, которые принимают значения от человека: `make meeting` и `make deliver`.

Ни одна не запускается по-настоящему: `make -n` печатает рецепт, а отказ без FILE и RUN
проверяется с подменённым PATH, где `uv` не найти.
"""

import shutil
import subprocess
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent


def printed(*goal: str) -> str:
    made = subprocess.run(
        ["make", "-n", *goal],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        check=True,
    )
    return made.stdout


def test_make_meeting_quotes_a_path_with_spaces() -> None:
    """Без кавычек «Новая запись 11.m4a» уходит в --meeting тремя аргументами."""
    assert '--meeting "Новая запись 11.m4a"' in printed("meeting", "FILE=Новая запись 11.m4a")


def test_make_deliver_quotes_the_run_it_was_given() -> None:
    assert '--deliver "a b"' in printed("deliver", "RUN=a b")


def test_make_meeting_without_a_recording_or_a_run_refuses(tmp_path: Path) -> None:
    """Пустой --meeting уводил argparse в жалобу на неизвестный аргумент вместо честного отказа."""
    make = shutil.which("make")
    assert make is not None
    made = subprocess.run(
        [make, "meeting"],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        # Рецепт дальше зовёт uv: с пустым PATH сломанная проверка не дошла бы до настоящего
        # прогона, а провалила бы тест другим кодом возврата.
        env={"PATH": str(tmp_path)},
    )

    # Свой код make печатает строкой «Error 64», а наружу отдаёт собственный.
    assert made.returncode != 0
    assert "Error 64" in made.stderr
    assert "FILE=" in made.stdout and "RUN=" in made.stdout
