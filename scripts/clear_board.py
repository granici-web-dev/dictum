"""Уборка своей доски после проверочных прогонов (P2-06).

Карточки уходят в архив, а не под нож: архивная карточка исчезает с доски и не участвует в
поиске маркеров при публикации, то есть для прогона это чистый лист, но любую можно вернуть.
Запускается руками и всегда спрашивает подтверждение: доска общая, и промах виден всей команде.

    uv run python -m scripts.clear_board --all
    uv run python -m scripts.clear_board --run 36c18be3f51087a6
"""

import argparse
import sys
import time
from typing import Any

from app.publish import MARKER
from app.trello import Trello, open_trello

# Trello отдаёт сотню запросов на десять секунд; пауза дешевле, чем ловить 429 на середине уборки.
PAUSE_SECONDS = 0.2

CONFIRMATION = "да"


def run_of(card: dict[str, Any]) -> str | None:
    """Прогон, оставивший карточку. Берётся последний маркер: строку мог процитировать и текст."""
    found = list(MARKER.finditer(card.get("desc", "")))
    return found[-1].group("run") if found else None


def chosen(cards: list[dict[str, Any]], run_id: str | None) -> list[dict[str, Any]]:
    """Без run_id — всё, что есть; с run_id — только карточки этого прогона."""
    if run_id is None:
        return cards
    return [card for card in cards if run_of(card) == run_id]


def cards_of_board(board: Trello) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = board.get(f"/boards/{board.board_id}/cards", fields="name,desc")
    return cards


def confirmed(question: str) -> bool:
    print(question)
    return input(f"Введите «{CONFIRMATION}», чтобы продолжить: ").strip().lower() == CONFIRMATION


def archive_cards(board: Trello, cards: list[dict[str, Any]]) -> None:
    for card in cards:
        board.send("PUT", f"/cards/{card['id']}/closed", fields={"value": "true"})
        print(f"  в архив: {card['name'][:70]}")
        time.sleep(PAUSE_SECONDS)


def archive_lists(board: Trello) -> None:
    for one in board.lists():
        board.send("PUT", f"/lists/{one.id}/closed", fields={"value": "true"})
        print(f"  в архив список: {one.name[:70]}")
        time.sleep(PAUSE_SECONDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Убрать карточки с доски в архив.")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true", help="все карточки и все списки доски")
    scope.add_argument("--run", metavar="RUN_ID", help="только карточки одного прогона")
    args = parser.parse_args(argv)

    board = open_trello()
    try:
        cards = chosen(cards_of_board(board), args.run)
        if not cards:
            print("Нечего убирать: под условие не попала ни одна карточка.")
            return 0
        what = "все карточки доски и списки" if args.all else f"карточки прогона {args.run}"
        if not confirmed(f"В архив уйдут {what}: {len(cards)} шт. Вернуть их можно из архива."):
            print("Отменено, доска не тронута.")
            return 1
        archive_cards(board, cards)
        if args.all:
            archive_lists(board)
        print(f"Готово: {len(cards)} карточек в архиве.")
    finally:
        board.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
