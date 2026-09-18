"""Контракт ресёрча: режим по снимку, сверка ссылок и цитат стандартов (SPEC §5, §7).

Фикстуры `approach_existing_de.json` и `approach_new_de.json` составлены вручную под поручение
`assignment_de.json`: живого прогона стадии ещё не было, и выдавать придуманный вывод за
перехваченный нельзя (`CLAUDE.md` §5).
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from app.answers import read_answers
from app.approach import (
    Approach,
    check_approach,
    leaked_in_queries,
    library_mentioned,
    normalized_url,
    quote_in,
    stamp_approach,
    unverified_problems,
)
from app.pipeline import APPROACH_SKIPPED
from app.project import Project, project_snapshot, read_project, read_standards
from app.render import approach_markdown
from app.review import Review
from app.steps import Assignment
from tests.helpers import FIXTURES

TAKEN_AT = datetime(2026, 9, 18, 9, 0, tzinfo=UTC)

ASSIGNMENT_DE = (FIXTURES / "assignment_de.json").read_text(encoding="utf-8")
ASSIGNMENT = Assignment.model_validate_json(ASSIGNMENT_DE)
PARTIAL_ANSWERS = read_answers((FIXTURES / "answers_de_partial.md").read_text(encoding="utf-8"))
# Так же, как склеивает их `run_stage`: и названный стек, и библиотека варианта ищутся в обоих.
SAID = f"{ASSIGNMENT_DE}\n{PARTIAL_ANSWERS.text}"
SAID_WITHOUT_ANSWERS = f"{ASSIGNMENT_DE}\n"

FRONTEND = read_project(project_snapshot(read_standards(FIXTURES / "project_frontend"), TAKEN_AT))
NO_STANDARDS = read_project(project_snapshot(None, TAKEN_AT))
CONVENTIONS = read_project(
    project_snapshot(read_standards(FIXTURES.parent / ".agents" / "context"), TAKEN_AT)
)

EXISTING_DE = (FIXTURES / "approach_existing_de.json").read_text(encoding="utf-8")
NEW_DE = (FIXTURES / "approach_new_de.json").read_text(encoding="utf-8")

EXISTING_URLS = {
    "https://example.org/react-hook-form/resolvers",
    "https://example.org/zod/strings",
}
NEW_URLS = {
    "https://example.org/react-hook-form/resolvers",
    "https://example.org/zod/changelog",
    "https://example.org/blog/forms-in-2026",
}
SEARCHES = ["react hook form zod resolver validation", "zod schema email required field"]


def changed(source: str, change: Callable[[dict[str, Any]], None]) -> str:
    data: dict[str, Any] = json.loads(source)
    change(data)
    return json.dumps(data, ensure_ascii=False)


def claims(
    approach_json: str,
    project: Project = FRONTEND,
    urls: set[str] | None = None,
    said: str = SAID,
) -> list[str]:
    return unverified_problems(
        approach_json, EXISTING_URLS if urls is None else urls, ASSIGNMENT, said, project
    )


def test_the_hand_written_fixtures_pass_their_own_checks() -> None:
    assert check_approach(EXISTING_DE) == []
    assert claims(EXISTING_DE) == []
    assert check_approach(NEW_DE) == []
    assert claims(NEW_DE, NO_STANDARDS, NEW_URLS, SAID_WITHOUT_ANSWERS) == []


def test_the_skipped_file_of_the_pipeline_passes_the_schema() -> None:
    """Пропуск ресёрча кладёт этот файл вместо вызова модели, и шаги читают его наравне."""
    skipped = Approach.model_validate_json(APPROACH_SKIPPED)

    assert (skipped.status, skipped.options, skipped.recommendation) == ("skipped", [], None)
    assert check_approach(APPROACH_SKIPPED) == []


def test_a_rejected_technology_in_an_option_is_claimed_and_never_forgiven() -> None:
    """Предлагать отвергнутое стандартами прямо нарушает решение владельца: это роняет стадию."""
    with_celery = changed(EXISTING_DE, lambda data: data["options"][0]["uses"].append("Celery"))
    rejected_celery = changed(
        with_celery,
        lambda data: data["rejected"].append(
            {"name": "Celery", "quote": "**Celery and Redis, until a second process exists.**"}
        ),
    )

    assert check_approach(rejected_celery) == [
        "options.0: Celery отвергнута в стандартах проекта "
        "(«**Celery and Redis, until a second process exists.**»): такого варианта не предлагай"
    ]


def test_a_library_of_this_repository_is_found_in_its_own_standards() -> None:
    """Стандарты этого репозитория — настоящие стандарты настоящего проекта, а не выдумка."""
    httpx = changed(
        EXISTING_DE,
        lambda data: data["options"][0].update({"uses": ["httpx"], "adds": [], "sources": ["S1"]}),
    )
    aiohttp = changed(
        httpx, lambda data: data["options"][0].update({"uses": ["aiohttp"]})
    )

    assert not [problem for problem in claims(httpx, CONVENTIONS) if "httpx" in problem]
    assert "options.0.uses: aiohttp нет ни в STACK.md, ни в поручении, ни в ответах" in " ".join(
        claims(aiohttp, CONVENTIONS)
    )


def test_a_library_already_in_the_standards_does_not_belong_in_adds() -> None:
    added = changed(
        EXISTING_DE,
        lambda data: data["options"][1].update(
            {"adds": ["React Hook Form"], "adds_why": "нужна форма"}
        ),
    )

    assert "options.1.adds: React Hook Form упомянута в STACK.md" in " ".join(claims(added))


def test_a_new_dependency_with_a_reason_is_fine() -> None:
    added = changed(
        EXISTING_DE,
        lambda data: data["options"][1].update(
            {"adds": ["yup"], "adds_why": "без схемы правила не переиспользовать"}
        ),
    )

    assert claims(added) == []


def test_a_hyphenated_name_finds_the_words_of_the_standards() -> None:
    """«react-hook-form» и «React Hook Form» это одно имя, а «zod» внутри «zodResolver» — нет."""
    assert library_mentioned("react-hook-form", "| Forms | React Hook Form | 7.x |")
    assert library_mentioned("i18next", "| Translations | i18next with react-i18next | 23.x |")
    assert not library_mentioned("zod", "подключить через zodResolver")


def test_a_quote_that_is_not_in_the_named_standard_is_claimed_and_then_marked() -> None:
    invented = changed(
        EXISTING_DE,
        lambda data: data["recommendation"]["standards_refs"][0].update(
            {"quote": "**Forms go through Formik.**"}
        ),
    )

    assert "recommendation.standards_refs.0: цитаты нет в STACK.md снимка" in " ".join(
        claims(invented)
    )
    stamped = Approach.model_validate_json(
        stamp_approach(invented, ASSIGNMENT, SAID, FRONTEND, EXISTING_URLS, SEARCHES)
    )
    assert stamped.recommendation is not None
    assert stamped.recommendation.standards_refs[0].in_snapshot is False


def test_a_recommendation_without_a_standards_reference_is_claimed() -> None:
    bare = changed(EXISTING_DE, lambda data: data["recommendation"].update({"standards_refs": []}))

    assert "recommendation.standards_refs: у проекта есть стандарты" in " ".join(claims(bare))


def test_a_quote_is_found_across_a_wrapped_line() -> None:
    assert quote_in("a b c", "a\n  b   c")
    assert not quote_in("a b d", "a b c")


@pytest.mark.parametrize(
    ("place", "change"),
    [
        ("rejected", lambda data: data["rejected"].append({"name": "Formik", "quote": "нет"})),
        ("options.0.adds", lambda data: data["options"][0].update({"adds": ["yup"]})),
        (
            "recommendation.standards_refs",
            lambda data: data["recommendation"].update(
                {"standards_refs": [{"file": "STACK.md", "quote": "нет"}]}
            ),
        ),
    ],
)
def test_a_new_project_has_no_standards_to_lean_on(
    place: str, change: Callable[[dict[str, Any]], None]
) -> None:
    broken = changed(NEW_DE, change)

    assert place in " ".join(claims(broken, NO_STANDARDS, NEW_URLS, SAID_WITHOUT_ANSWERS))


def test_a_new_project_without_a_named_stack_must_ask_about_it() -> None:
    silent = changed(NEW_DE, lambda data: data.update({"new_questions": []}))

    assert "new_questions: стека не назвали ни поручение, ни ответы" in " ".join(
        claims(silent, NO_STANDARDS, NEW_URLS, SAID_WITHOUT_ANSWERS)
    )


def test_a_stack_named_in_the_answers_stops_the_recommendation_being_provisional() -> None:
    named = changed(
        NEW_DE,
        lambda data: data.update({"stack_quote": "Nehmt React Hook Form mit zod"}),
    )

    stamped = Approach.model_validate_json(
        stamp_approach(named, ASSIGNMENT, SAID, NO_STANDARDS, NEW_URLS, SEARCHES)
    )

    assert stamped.stack_named is True
    assert stamped.recommendation is not None
    assert stamped.recommendation.provisional is False
    assert claims(named, NO_STANDARDS, NEW_URLS) == []


def test_the_mode_is_stamped_by_the_snapshot_whatever_the_model_sent() -> None:
    lying = changed(NEW_DE, lambda data: data.update({"mode": "existing"}))

    stamped = Approach.model_validate_json(
        stamp_approach(lying, ASSIGNMENT, SAID_WITHOUT_ANSWERS, NO_STANDARDS, NEW_URLS, SEARCHES)
    )

    assert stamped.mode == "new"
    assert stamped.recommendation is not None
    assert stamped.recommendation.provisional is True


def test_a_source_that_was_never_searched_is_claimed_and_then_marked() -> None:
    """Придуманная ссылка не отнимает весь оплаченный ресёрч, но и источником не считается."""
    invented = changed(
        EXISTING_DE,
        lambda data: data["sources"][1].update({"url": "https://example.org/never-searched"}),
    )

    assert "sources.1: ссылки https://example.org/never-searched нет в результатах" in " ".join(
        claims(invented)
    )
    stamped = Approach.model_validate_json(
        stamp_approach(invented, ASSIGNMENT, SAID, FRONTEND, EXISTING_URLS, SEARCHES)
    )
    assert [source.found_by_search for source in stamped.sources] == [True, False]


def test_the_url_comparison_ignores_case_fragment_and_trailing_slash() -> None:
    assert normalized_url("HTTPS://Example.org/a/#x") == normalized_url("https://example.org/a")


def test_a_recommendation_of_an_option_that_is_not_there_is_claimed() -> None:
    lost = changed(EXISTING_DE, lambda data: data["recommendation"].update({"option": "Formik"}))

    assert 'recommendation.option: варианта «Formik» нет в options' in " ".join(claims(lost))


def test_a_source_id_that_is_not_in_sources_is_claimed() -> None:
    dangling = changed(EXISTING_DE, lambda data: data["options"][0]["sources"].append("S9"))

    assert "options.0.sources: источника S9 нет в sources" in " ".join(claims(dangling))


def test_an_empty_headline_translation_is_claimed_when_the_meeting_was_not_in_owner_lang() -> None:
    untranslated = changed(
        EXISTING_DE, lambda data: data["recommendation"]["headline"].update({"translation": ""})
    )

    assert "recommendation.headline.translation: перевод пуст" in " ".join(claims(untranslated))


def test_a_ticket_key_or_host_in_a_search_query_is_named() -> None:
    """Запрос уже ушёл наружу: строка делает утечку видимой, а не предотвращает её (SPEC §8)."""
    review = Review.model_validate_json(
        (FIXTURES / "review_ticket_de.json").read_text(encoding="utf-8")
    )
    task = review.tasks[0]

    assert leaked_in_queries(["zod schema email"], task) == []
    assert leaked_in_queries([f"{task.ticket_key} zod schema"], task) == ["ticket_key"]
    assert leaked_in_queries(["react hook form jira.example.com"], task) == ["ticket_host"]


def test_approach_markdown_draws_the_existing_project_by_the_snapshot() -> None:
    drawn = approach_markdown(Approach.model_validate_json(EXISTING_DE), FRONTEND, SAID)

    assert drawn == (FIXTURES / "approach_existing_de.md").read_text(encoding="utf-8")


def test_the_marks_say_what_the_owner_cannot_take_on_trust() -> None:
    def everything_doubtful(data: dict[str, Any]) -> None:
        data["options"][0].update({"uses": ["aiohttp"], "sources": []})
        data["options"][1].update({"adds": ["yup"], "adds_why": "схема правил"})
        data["sources"][1].update({"found_by_search": False})
        data["recommendation"]["standards_refs"][0].update({"in_snapshot": False})
        data["recommendation"].update({"provisional": True})

    marked = changed(EXISTING_DE, everything_doubtful)

    drawn = approach_markdown(Approach.model_validate_json(marked), FRONTEND, SAID)

    assert "*(outside the project standards)*" in drawn
    assert "*(new dependency: the teamlead decides)*" in drawn
    assert "*(quote not found in the standards)*" in drawn
    assert "*(provisional: the stack is not confirmed)*" in drawn
    assert "*(not in the search results)*" in drawn
    assert "*(no source: the model's opinion)*" in drawn
