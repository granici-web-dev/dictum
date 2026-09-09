"""Порядок стадий: что после чего, что каждая читает и что отдаёт. См. SPEC.md §7.2.

Единственный ответ на вопрос «что после чего». Идущий по списку сам решает, с какой записи начать:
CLI — от стадии в `--from`, бот — от начала. Раньше порядок был записан трижды, и два места уже
разошлись в том, считать ли research стадией.
"""

from typing import Literal

from pydantic import BaseModel, Field

TRANSCRIPT = "inputs/transcript.md"
IDEA = "inputs/idea.md"
CANDIDATES = "outputs/candidates.md"
BRIEF = "outputs/brief.md"
RESEARCH = "outputs/research.md"
PRD = "outputs/prd.md"
ISSUES_JSON = "outputs/issues.json"
ISSUES_MD = "outputs/issues.md"

RESEARCH_SKIPPED = "Ресёрч не запускался: локальный прогон через make run-text.\n"


class Stage(BaseModel):
    name: str
    runs: Literal["llm", "code"]
    inputs: tuple[str, ...] = ()
    # Файл репозитория, а не артефакт прогона: его не читают из outputs/ и не версионируют.
    template: str | None = None
    # Что отдаёт исполнитель стадии. issues.md сюда не входит: его дорисовывает код
    # уже после проверок, и модель за него не отвечает.
    outputs: tuple[frozenset[str], ...]
    params: dict[str, str] = Field(default_factory=dict)
    # Язык — свойство прогона, а не стадии, поэтому подмешивается на ходу. Сегодня его получает
    # только brief: остальным промптам его не показывали, и менять их вход — отдельное решение.
    needs_lang: bool = False
    # Артефакт, который человек читает на воротах после стадии (SPEC §3.2); None — ворот нет.
    # Из outputs его не вывести: у decompose на воротах читают issues.md, а он там не объявлен.
    gate_after: str | None = None


STAGES: tuple[Stage, ...] = (
    Stage(
        name="ingest",
        runs="code",
        outputs=(frozenset({TRANSCRIPT}),),
    ),
    Stage(
        name="intake",
        runs="llm",
        inputs=(TRANSCRIPT,),
        outputs=(frozenset({IDEA}), frozenset({CANDIDATES})),
    ),
    Stage(
        name="brief",
        runs="llm",
        inputs=(IDEA,),
        outputs=(frozenset({BRIEF}),),
        params={"mode": "batch", "interactive": "false"},
        needs_lang=True,
        gate_after=BRIEF,
    ),
    # Пока ресёрча нет, стадия исполняется кодом и кладёт фиксированную строку: /prd просит файл на
    # вход. Формулировку пользовательского skip («по решению пользователя») здесь брать нельзя,
    # никто ничего не решал. Придёт P4-01 — запись станет llm и получит inputs.
    Stage(
        name="research",
        runs="code",
        outputs=(frozenset({RESEARCH}),),
    ),
    Stage(
        name="prd",
        runs="llm",
        inputs=(BRIEF, RESEARCH),
        template="prd_oneshot.md",
        outputs=(frozenset({PRD}),),
    ),
    Stage(
        name="decompose",
        runs="llm",
        inputs=(PRD,),
        outputs=(frozenset({ISSUES_JSON}),),
        gate_after=ISSUES_MD,
    ),
    # Карточки на доске — не артефакт прогона, и publish.json стадия пишет сама, по карточке за
    # раз, чтобы оборванная публикация оставила журнал: обходчику отдавать нечего.
    Stage(
        name="publish",
        runs="code",
        inputs=(ISSUES_JSON,),
        outputs=(frozenset(),),
    ),
)

NAMES = tuple(stage.name for stage in STAGES)


def stage_named(name: str) -> Stage:
    for stage in STAGES:
        if stage.name == name:
            return stage
    raise ValueError(f"unknown stage: {name}")


def produced_by(path: str) -> str | None:
    for stage in STAGES:
        if any(path in outputs for outputs in stage.outputs):
            return stage.name
    return None


def stages_between(start: str, stop: str) -> tuple[Stage, ...]:
    first = NAMES.index(stage_named(start).name)
    last = NAMES.index(stage_named(stop).name)
    return STAGES[first : last + 1]
