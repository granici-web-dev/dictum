from pathlib import Path

from app.candidates import check_candidates, parse_candidates

FIXTURES = Path(__file__).parent.parent / "fixtures"
MULTIPLE = (FIXTURES / "candidates_multiple.md").read_text(encoding="utf-8")
NONE = (FIXTURES / "candidates_none.md").read_text(encoding="utf-8")


def test_the_list_keeps_the_numbers_and_drops_the_bookkeeping() -> None:
    """Человек в чате выбирает по названию: кто сказал и какой статус — не его забота."""
    found = parse_candidates(MULTIPLE)

    assert found.outcome == "multiple"
    assert [(idea.number, idea.title) for idea in found.ideas] == [
        (1, "Бот для онбординга новичков"),
        (2, "Утренняя сводка по просроченным дедлайнам"),
    ]


def test_a_file_without_an_idea_is_told_apart_by_its_outcome() -> None:
    """Тот же файл и тот же список, но темы разговора, а не идеи: различает только outcome."""
    found = parse_candidates(NONE)

    assert found.outcome == "none"
    assert [idea.title for idea in found.ideas] == [
        "Сроки по текущему спринту",
        "Кто идёт на конференцию",
    ]


def test_both_shapes_from_the_prompt_pass_the_check() -> None:
    assert check_candidates(MULTIPLE) == []
    assert check_candidates(NONE) == []


def test_a_file_without_frontmatter_is_rejected() -> None:
    without = MULTIPLE.split("---\n")[-1]

    assert check_candidates(without) == ["outcome: None — во frontmatter нужно multiple или none"]


def test_an_unknown_outcome_is_rejected() -> None:
    assert check_candidates(MULTIPLE.replace("outcome: multiple", "outcome: maybe")) == [
        "outcome: 'maybe' — во frontmatter нужно multiple или none"
    ]


def test_prose_without_a_list_is_rejected() -> None:
    prose = "---\noutcome: none\n---\n\n# Идея не найдена\n\nВ записи только обсуждение.\n"

    problems = check_candidates(prose)

    assert problems == ["нет ни одной строки вида `N. **Название** — …` с начала строки"]


def test_a_nested_item_is_not_an_idea() -> None:
    """Подпункт — часть идеи, а не идея: с ним человеку показывали два пункта под номером 1."""
    nested = (
        "---\noutcome: multiple\n---\n\n# В записи найдено 2 идеи\n\n"
        "1. **Онбординг** — бот с чек-листом.\n"
        "   1. **Доступы** — отдельным шагом.\n"
        "2. **Сводка** — утреннее сообщение.\n"
    )

    assert check_candidates(nested) == []
    assert [idea.number for idea in parse_candidates(nested).ideas] == [1, 2]


def test_a_year_in_the_middle_of_a_sentence_is_not_an_idea() -> None:
    prose = (
        "---\noutcome: multiple\n---\n\n# В записи найдено 1 идея\n\n"
        "1. **Сводка** — утреннее сообщение.\n\nОн сказал, что\n2024. год был тяжёлым\n"
    )

    assert [idea.title for idea in parse_candidates(prose).ideas] == ["Сводка"]


def test_an_idea_without_a_bold_name_is_rejected() -> None:
    """Такая строка не доехала бы до чата, и человек выбирал бы из двух идей, зная одну."""
    half = MULTIPLE.replace(
        "2. **Утренняя сводка по просроченным дедлайнам** —", "2. Утренняя сводка —"
    )

    assert check_candidates(half) == ["название не выделено **жирным** в строках: 2"]


def test_a_gap_in_the_numbering_is_rejected() -> None:
    gapped = MULTIPLE.replace("2. **Утренняя", "3. **Утренняя")

    assert check_candidates(gapped) == ["номера идут не подряд с 1: 1, 3"]
