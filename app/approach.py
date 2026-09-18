"""Контракт outputs/approach.json: ресёрч решений по одному поручению. См. SPEC.md §5, §7.

Режим выбирает не модель, а код по снимку стандартов: есть у проекта STACK.md — варианты ищутся
только внутри его стека, нет — подбирается стек, как для нового проекта. Догадка «похоже на
существующий проект» проверяется только глазами, а флаг снимка — тестом.

Ссылки сверяются с тем, что правда пришло из поиска, цитаты стандартов — с текстом снимка: вывод
без подтверждённого источника это мнение модели, и выглядеть он должен мнением, а не источником.
"""

import json
import re
from typing import Literal, Self
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.clarify import Question
from app.project import Project
from app.review import NOT_BLANK, Task
from app.steps import Assignment, Pair

Mode = Literal["existing", "new"]
KnownFrom = Literal["standards", "task", "answers"]

MIN_OPTIONS = 1
MAX_OPTIONS = 4

SOURCE_ID = r"^S[1-9]\d*$"

# Слово это буквы и цифры подряд, всё остальное разделитель: разделители в именах библиотек пишут
# по-разному («react-hook-form», «React Hook Form»), а совпадать должно имя целиком, а не кусок
# слова — иначе `zod` находился бы внутри `zodResolver`.
LIBRARY_WORD = re.compile(r"[^\W_]+")


class Known(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(pattern=NOT_BLANK)
    origin: KnownFrom = Field(alias="from")


class Rejected(BaseModel):
    """Технология, отвергнутая стандартами проекта, с дословной строкой стандартов."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NOT_BLANK)
    quote: str = Field(pattern=NOT_BLANK)


class Option(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NOT_BLANK)
    summary: str = Field(pattern=NOT_BLANK)
    # Библиотеки и инструменты варианта, в оригинале. `adds` — то, чего в стеке проекта нет.
    uses: list[str] = Field(default_factory=list)
    adds: list[str] = Field(default_factory=list)
    adds_why: str | None = None
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    cost: str = Field(pattern=NOT_BLANK)
    sources: list[str] = Field(default_factory=list)


class StandardsRef(BaseModel):
    """Строка стандартов проекта, на которую опирается рекомендация."""

    model_config = ConfigDict(extra="forbid")

    file: str = Field(pattern=NOT_BLANK)
    quote: str = Field(pattern=NOT_BLANK)
    in_snapshot: bool | None = None


class Recommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    option: str = Field(pattern=NOT_BLANK)
    why: str = Field(pattern=NOT_BLANK)
    # 3–6 практик: как это правильно писать на выбранном. Их читают шаги.
    how_to_write: list[str] = Field(default_factory=list)
    standards_refs: list[StandardsRef] = Field(default_factory=list)
    provisional: bool | None = None
    # Одна строка на языке встречи с переводом: из неё вырастает строка подхода на воротах шагов.
    headline: Pair
    sources: list[str] = Field(default_factory=list)


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=SOURCE_ID)
    url: str = Field(pattern=NOT_BLANK)
    title: str = Field(pattern=NOT_BLANK)
    found_by_search: bool | None = None


class Approach(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["done", "skipped"] = "done"
    mode: Mode | None = None
    # Только у нового проекта: где стек назван дословно, в поручении или в ответах тимлида.
    stack_quote: str | None = None
    stack_named: bool | None = None
    known: list[Known] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)
    rejected: list[Rejected] = Field(default_factory=list)
    options: list[Option] = Field(default_factory=list)
    recommendation: Recommendation | None = None
    new_questions: list[Question] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    searches: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def a_finished_research_has_options_and_one_recommendation(self) -> Self:
        if self.status == "skipped":
            if self.options or self.recommendation is not None:
                raise ValueError("у пропущенного ресёрча нет ни вариантов, ни рекомендации")
        elif not MIN_OPTIONS <= len(self.options) <= MAX_OPTIONS or self.recommendation is None:
            raise ValueError(
                f"у сделанного ресёрча от {MIN_OPTIONS} до {MAX_OPTIONS} вариантов "
                "и одна рекомендация"
            )
        return self


def library_words(text: str) -> list[str]:
    return LIBRARY_WORD.findall(text.casefold())


def library_mentioned(name: str, text: str) -> bool:
    """Имя библиотеки названо в тексте целыми словами, как бы их ни разделяли."""
    wanted = library_words(name)
    if not wanted:
        return False
    words = library_words(text)
    return any(
        words[start : start + len(wanted)] == wanted
        for start in range(len(words) - len(wanted) + 1)
    )


def same_library(one: str, other: str) -> bool:
    return library_words(one) == library_words(other) and bool(library_words(one))


def quote_in(quote: str, text: str) -> bool:
    """Цитата дословно есть в тексте. Пробелы схлопнуты: перенос строки не слово."""
    return " ".join(quote.split()) in " ".join(text.split())


def normalized_url(url: str) -> str:
    """URL для сравнения: схема и хост в нижнем регистре, без фрагмента и хвостового «/»."""
    parts = urlsplit(url.strip())
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, "")
    )


def mode_of(project: Project) -> Mode:
    """Режим ресёрча: со STACK.md работаем внутри стека проекта, без него подбираем стек."""
    return "existing" if project.stack else "new"


def standard_text(project: Project, name: str) -> str:
    """Текст названного стандарта в снимке; пусто, если такого файла в снимке нет."""
    return next((text for found, text in project.texts.items() if found == name), "")


def stack_text(project: Project) -> str:
    return standard_text(project, "STACK.md")


def schema_problems(approach_json: str) -> list[str]:
    try:
        parsed = json.loads(approach_json)
    except json.JSONDecodeError as error:
        return [f"не разбирается как JSON: {error}"]
    try:
        Approach.model_validate(parsed)
    except ValidationError as error:
        return [
            f"{'.'.join(str(part) for part in problem['loc'])}: {problem['msg']}"
            for problem in error.errors()
        ]
    return []


def check_approach(approach_json: str) -> list[str]:
    """Претензии, которые роняют стадию и после ремонта: форма и отвергнутая технология.

    Отвергнутое в стандартах, предложенное вариантом, пометкой не лечится: это прямо нарушает
    решение владельца о том, что ресёрч существующего проекта не выходит за его стек.
    """
    problems = schema_problems(approach_json)
    if problems:
        return problems
    approach = Approach.model_validate_json(approach_json)
    return [
        f"options.{index}: {name} отвергнута в стандартах проекта («{rejected.quote}»): "
        "такого варианта не предлагай"
        for index, option in enumerate(approach.options)
        for name in [*option.uses, *option.adds]
        for rejected in approach.rejected
        if same_library(name, rejected.name)
    ]


def stack_problems(approach: Approach, project: Project, assignment_and_answers: str) -> list[str]:
    stack = stack_text(project)
    problems = []
    for index, option in enumerate(approach.options):
        problems += [
            f"options.{index}.uses: {name} нет ни в STACK.md, ни в поручении, ни в ответах: "
            "перенеси в adds с причиной или убери"
            for name in option.uses
            if not (
                library_mentioned(name, stack)
                or library_mentioned(name, assignment_and_answers)
            )
        ]
        problems += [
            f"options.{index}.adds: {name} упомянута в STACK.md: либо она в стеке (uses), "
            "либо отвергнута и не предлагается"
            for name in option.adds
            if library_mentioned(name, stack)
        ]
    return problems


def quote_problems(approach: Approach, project: Project) -> list[str]:
    stack = stack_text(project)
    problems = [
        f"rejected.{index}.quote: этой строки нет в STACK.md снимка: скопируй её буква в букву"
        for index, rejected in enumerate(approach.rejected)
        if not quote_in(rejected.quote, stack)
    ]
    if approach.recommendation is None:
        return problems
    references = approach.recommendation.standards_refs
    if not references:
        return [
            *problems,
            "recommendation.standards_refs: у проекта есть стандарты, и рекомендация опирается "
            "хотя бы на одну их строку",
        ]
    return problems + [
        f"recommendation.standards_refs.{index}: цитаты нет в {reference.file} снимка: "
        "скопируй строку стандартов буква в букву"
        for index, reference in enumerate(references)
        if not quote_in(reference.quote, standard_text(project, reference.file))
    ]


def new_project_problems(approach: Approach, assignment_and_answers: str) -> list[str]:
    problems = [
        f"options.{index}.adds: у нового проекта весь стек новый, отдельных adds не бывает"
        for index, option in enumerate(approach.options)
        if option.adds
    ]
    if approach.rejected:
        problems.append("rejected: стандартов проекта нет, отвергать в них нечего")
    if approach.recommendation and approach.recommendation.standards_refs:
        problems.append(
            "recommendation.standards_refs: стандартов проекта нет, ссылаться не на что"
        )
    named = approach.stack_quote is not None and quote_in(
        approach.stack_quote, assignment_and_answers
    )
    if not named and not approach.new_questions:
        problems.append(
            "new_questions: стека не назвали ни поручение, ни ответы, и вопрос о стеке обязателен"
        )
    return problems


def cited_sources(approach: Approach) -> list[tuple[str, str]]:
    """Где какой источник назван: место и его идентификатор."""
    places = [
        (f"options.{index}.sources", identifier)
        for index, option in enumerate(approach.options)
        for identifier in option.sources
    ]
    if approach.recommendation:
        places += [
            ("recommendation.sources", identifier)
            for identifier in approach.recommendation.sources
        ]
    return places


def source_problems(approach: Approach, seen_urls: set[str]) -> list[str]:
    seen = {normalized_url(url) for url in seen_urls}
    known = {source.id for source in approach.sources}
    problems = [
        f"sources.{index}: ссылки {source.url} нет в результатах поиска: убери её или замени "
        "найденной"
        for index, source in enumerate(approach.sources)
        if normalized_url(source.url) not in seen
    ]
    problems += [
        f"{place}: источника {identifier} нет в sources"
        for place, identifier in cited_sources(approach)
        if identifier not in known
    ]
    if approach.recommendation and approach.recommendation.option not in {
        option.name for option in approach.options
    }:
        problems.append(
            f"recommendation.option: варианта «{approach.recommendation.option}» нет в options"
        )
    return problems


def translation_problems(approach: Approach, assignment: Assignment) -> list[str]:
    meeting_lang, owner_lang = assignment.meeting_lang, assignment.owner_lang
    if meeting_lang is None or meeting_lang == owner_lang:
        return []
    untranslated = [
        (f"new_questions.{index}", question.translation)
        for index, question in enumerate(approach.new_questions)
    ]
    if approach.recommendation:
        untranslated.append(
            ("recommendation.headline", approach.recommendation.headline.translation)
        )
    return [
        f"{place}.translation: перевод пуст, а встреча на {meeting_lang}, язык владельца "
        f"{owner_lang}"
        for place, translation in untranslated
        if not (translation and translation.strip())
    ]


def unverified_problems(
    approach_json: str,
    seen_urls: set[str],
    assignment: Assignment,
    assignment_and_answers: str,
    project: Project,
) -> list[str]:
    """Претензии первого ответа, которые после ремонта становятся пометками (SPEC §7).

    Придуманная ссылка или неточная цитата не отнимают весь оплаченный ресёрч: они перестают
    выдаваться за источник, тем же приёмом, каким помечается несошедшийся фрагмент разбора.
    """
    try:
        approach = Approach.model_validate_json(approach_json)
    except ValidationError:
        # Сломанную форму уже назвал check_approach: сверять нечего.
        return []
    if mode_of(project) == "existing":
        problems = stack_problems(approach, project, assignment_and_answers)
        problems += quote_problems(approach, project)
    else:
        problems = new_project_problems(approach, assignment_and_answers)
    return problems + source_problems(approach, seen_urls) + translation_problems(
        approach, assignment
    )


def stamp_approach(
    approach_json: str,
    assignment: Assignment,
    assignment_and_answers: str,
    project: Project,
    seen_urls: set[str],
    searches: list[str],
) -> str:
    """Готовый ресёрч: режим, находки и запросы вписаны кодом, присланное в этих полях затёрто."""
    approach = Approach.model_validate_json(approach_json)
    approach.mode = mode_of(project)
    approach.stack_named = approach.stack_quote is not None and quote_in(
        approach.stack_quote, assignment_and_answers
    )
    seen = {normalized_url(url) for url in seen_urls}
    for source in approach.sources:
        source.found_by_search = normalized_url(source.url) in seen
    if approach.recommendation is not None:
        approach.recommendation.provisional = approach.mode == "new" and not approach.stack_named
        for reference in approach.recommendation.standards_refs:
            reference.in_snapshot = quote_in(
                reference.quote, standard_text(project, reference.file)
            )
    approach.searches = searches
    return approach.model_dump_json(indent=2, by_alias=True) + "\n"


def unverified_sources(approach: Approach) -> int:
    return sum(source.found_by_search is not True for source in approach.sources)


def confirmed_source_ids(approach: Approach) -> set[str]:
    """Источники, найденные поиском: всё остальное рисуется как мнение модели."""
    return {source.id for source in approach.sources if source.found_by_search is True}


def outside_the_standards(option: Option, project: Project, assignment_and_answers: str) -> bool:
    """Вариант назвал библиотеку, которой нет ни в стандартах, ни в сказанном (SPEC §7)."""
    stack = stack_text(project)
    return any(
        not (library_mentioned(name, stack) or library_mentioned(name, assignment_and_answers))
        for name in option.uses
    )


def leaked_in_queries(queries: list[str], task: Task) -> list[str]:
    """Что из тикета ушло в запрос поиска: ключ или хост ссылки.

    Запрос к этому моменту уже ушёл наружу: строка делает утечку видимой, а не предотвращает её.
    Помешать ей может только промпт — серверный инструмент не даёт коду увидеть запрос до поиска,
    а списка названий компании и её продуктов у кода нет вовсе (SPEC §8).
    """
    found = []
    key = task.ticket_key
    if key and any(key.casefold() in query.casefold() for query in queries):
        found.append("ticket_key")
    host = urlsplit(task.ticket_url).netloc.casefold() if task.ticket_url else ""
    if host and any(host in query.casefold() for query in queries):
        found.append("ticket_host")
    return found
