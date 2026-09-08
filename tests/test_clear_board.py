from typing import Any

from scripts.clear_board import chosen, run_of

MINE = "36c18be3f51087a6"
OTHER = "0ac7bdffe0e0ba58"


def a_card(name: str, run_id: str | None) -> dict[str, Any]:
    marker = f"\n\ndictum:DCT-7 run:{run_id} local:I-001" if run_id else ""
    return {"id": name, "name": name, "desc": f"Описание карточки.{marker}"}


def test_a_card_belongs_to_the_run_named_by_its_last_marker() -> None:
    """Строку dictum: мог процитировать и сам текст issue, поэтому берётся последняя."""
    quoted = a_card("цитата", MINE)
    quoted["desc"] = f"Раньше было dictum:DCT-1 run:{OTHER} local:I-001\n{quoted['desc']}"

    assert run_of(quoted) == MINE


def test_a_card_without_a_marker_belongs_to_nobody() -> None:
    assert run_of(a_card("чужая", None)) is None


def test_cleaning_one_run_leaves_the_cards_of_the_others() -> None:
    cards = [a_card("моя", MINE), a_card("чужая", OTHER), a_card("ничья", None)]

    assert [card["name"] for card in chosen(cards, MINE)] == ["моя"]


def test_cleaning_everything_takes_the_cards_nobody_claims() -> None:
    """Карточка без маркера сделана руками; при уборке всей доски она уходит вместе со всеми."""
    cards = [a_card("моя", MINE), a_card("ничья", None)]

    assert len(chosen(cards, None)) == 2
