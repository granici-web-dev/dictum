"""Порядок стадий: что после чего, что каждая читает и что отдаёт. См. SPEC.md §7.2.

Единственный ответ на вопрос «что после чего». Идущий по списку сам решает, с какой записи начать:
CLI — от стадии в `--from`, бот — от начала. Раньше порядок был записан трижды, и два места уже
разошлись в том, считать ли research стадией.
"""

from typing import Literal

from pydantic import BaseModel, Field

# Род остановки обхода (SPEC §3.2, §3.3). Объявлен здесь, потому что читают его с двух сторон:
# `Pause` и `Redo` в app/run.py и `Stopped` в app/store.py, а общий у них только этот модуль.
StopKind = Literal["choice", "gate", "answer"]
# Пауза обхода шире остановки: вопросы для тимлида ждут ответа днями и не занимают места остановки
# в чате (P3-11), поэтому строке они не остановка, а обходу такой же выход, как остальные.
PauseKind = StopKind | Literal["questions"]

TRANSCRIPT = "inputs/transcript.md"
REVIEW_JSON = "outputs/review.json"
REVIEW_MD = "outputs/review.md"
IDEA = "inputs/idea.md"
CANDIDATES = "outputs/candidates.md"
BRIEF = "outputs/brief.md"
BRIEF_QUESTION = "outputs/brief_question.md"
RESEARCH = "outputs/research.md"
PRD = "outputs/prd.md"
ISSUES_JSON = "outputs/issues.json"
ISSUES_MD = "outputs/issues.md"
ASSIGNMENT_JSON = "inputs/assignment.json"
PROJECT = "inputs/project.md"
CLARIFY_JSON = "outputs/clarify.json"
ANSWERS = "inputs/answers.md"
STEPS_JSON = "outputs/steps.json"
STEPS_MD = "outputs/steps.md"

RESEARCH_SKIPPED = "Ресёрч не запускался: локальный прогон через make run-text.\n"


class Stage(BaseModel):
    name: str
    runs: Literal["llm", "code"]
    inputs: tuple[str, ...] = ()
    # Файл репозитория, а не артефакт прогона: его не читают из outputs/ и не версионируют.
    template: str | None = None
    # Что отдаёт исполнитель стадии. issues.md и review.md сюда не входят: их дорисовывает код
    # уже после проверок, и модель за них не отвечает.
    outputs: tuple[frozenset[str], ...]
    params: dict[str, str] = Field(default_factory=dict)
    # Язык — свойство прогона, а не стадии, поэтому подмешивается на ходу. Сегодня его получает
    # только brief: остальным промптам его не показывали, и менять их вход — отдельное решение.
    needs_lang: bool = False
    # То же и с `interactive`: есть ли кому отвечать, знает прогон, а не запись стадии. Пока
    # диалога не было, флаг стоял здесь константой (SPEC §3.2); с P2-04 его называет вход.
    needs_interactive: bool = False
    # Язык владельца — свойство установки, а не прогона (P3-08): его читают из настроек в момент
    # стадии, а дальше язык объявляет её артефакт.
    needs_owner_lang: bool = False
    # Артефакт, который человек читает на воротах после стадии (SPEC §3.2); None — ворот нет.
    # Из outputs его не вывести: у decompose на воротах читают issues.md, а он там не объявлен.
    gate_after: str | None = None
    # Сколько модели размышлять на этой стадии, словами API (`thinking.type`, SPEC §7). None —
    # параметр не отправляется вовсе, и стадия остаётся ровно такой, какой была. `adaptive` это
    # то же самое, сказанное явно, `disabled` — минимально возможное.
    thinking: Literal["adaptive", "disabled"] | None = None


STAGES: tuple[Stage, ...] = (
    Stage(
        name="ingest",
        runs="code",
        outputs=(frozenset({TRANSCRIPT}),),
    ),
    Stage(
        name="review",
        runs="llm",
        inputs=(TRANSCRIPT,),
        outputs=(frozenset({REVIEW_JSON}),),
        needs_owner_lang=True,
    ),
    # Путь идеи в боте начинается с разбора-родителя: расшифровка у него уже есть, и второй
    # Whisper по той же записи оплатил бы то, что уже оплачено.
    Stage(
        name="handoff",
        runs="code",
        outputs=(frozenset({TRANSCRIPT}),),
    ),
    Stage(
        name="intake",
        runs="llm",
        inputs=(TRANSCRIPT,),
        outputs=(frozenset({IDEA}), frozenset({CANDIDATES})),
    ),
    # Две ветки выхода, как у intake: бриф или очередной вопрос человеку (SPEC §3.3). Вопрос
    # останавливает обход и приходит правкой обратно в эту же стадию.
    Stage(
        name="brief",
        runs="llm",
        inputs=(IDEA,),
        outputs=(frozenset({BRIEF}), frozenset({BRIEF_QUESTION})),
        params={"mode": "batch"},
        needs_lang=True,
        needs_interactive=True,
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
    # Поручение из разбора родителя собирает код, без вызова модели и без соседних поручений:
    # стадии шагов нужно одно поручение, а не вся встреча.
    # Снимок стандартов рабочего проекта пишется тем же ходом: прогон помнит, по каким стандартам
    # его собирали, и `--from steps` через неделю идёт по тому же снимку.
    Stage(
        name="assignment",
        runs="code",
        outputs=(frozenset({ASSIGNMENT_JSON, PROJECT}),),
    ),
    # Вопросы для тимлида до шагов: ожидание ответа, которое длится днями, начинается сразу.
    Stage(
        name="clarify",
        runs="llm",
        inputs=(ASSIGNMENT_JSON, PROJECT),
        outputs=(frozenset({CLARIFY_JSON}),),
    ),
    # Ответ тимлида пишется всегда, одним из четырёх состояний, и снимок стандартов переснимается:
    # между вопросами и ответом проходят дни, и каталог могли поправить.
    Stage(
        name="answers",
        runs="code",
        inputs=(CLARIFY_JSON, PROJECT),
        outputs=(frozenset({ANSWERS, PROJECT}),),
    ),
    # Язык встречи стадия берёт из assignment.json, а не `lang` прогона: у текста это не язык.
    Stage(
        name="steps",
        runs="llm",
        inputs=(ASSIGNMENT_JSON, CLARIFY_JSON, ANSWERS, PROJECT),
        outputs=(frozenset({STEPS_JSON}),),
        gate_after=STEPS_MD,
    ),
    # Поручение ложится на доску одной карточкой; publish.json стадия пишет сама, как publish.
    Stage(
        name="card",
        runs="code",
        inputs=(STEPS_JSON,),
        outputs=(frozenset(),),
    ),
)

NAMES = tuple(stage.name for stage in STAGES)

# Маршруты: отрезки списка от первой стадии до последней включительно, у каждого свой результат.
# Обход идёт до конца маршрута, в котором стоит стартовая стадия: так продолженный прогон доходит
# до того же результата, к которому шёл, а не до конца всего списка (P3-08).
# Запись кончается разбором встречи. Путь идеи начинается с handoff, который берёт расшифровку у
# разбора-родителя; `make run-text --from intake` идёт по лежащей расшифровке и его минует. Путь
# поручения начинается с assignment и кончается карточкой.
ROUTES: tuple[tuple[str, str], ...] = (
    ("ingest", "review"),
    ("handoff", "publish"),
    ("assignment", "card"),
)
# Стадии, которые пишут на доску. Локальный прогон их не выполняет: публикация тратит деньги на
# доске и заслуживает собственной команды.
PUBLISHING = frozenset({"publish", "card"})


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


def route_of(name: str) -> tuple[str, str]:
    position = NAMES.index(stage_named(name).name)
    for first, last in ROUTES:
        if NAMES.index(first) <= position <= NAMES.index(last):
            return first, last
    raise ValueError(f"стадия {name} не входит ни в один маршрут")


def route_end(name: str) -> str:
    return route_of(name)[1]


def after(name: str) -> Stage:
    """Следующая запись: откуда идти дальше прогону, подтверждённому на воротах.

    Ворот после последней стадии маршрута не бывает — обход их не отдаёт, — поэтому вопрос «что
    после card» может задать только ошибка в данных, и отвечать на неё `None` значит завести у
    вызывающего ветку, которой неоткуда случиться.
    """
    following = NAMES.index(stage_named(name).name) + 1
    if following == len(STAGES):
        raise ValueError(f"после стадии {name} других нет")
    return STAGES[following]
