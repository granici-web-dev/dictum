"""Обход пайплайна: одна дорога для make run-text и для бота. См. SPEC.md §7.2.

Список стадий лежит в app/pipeline.py и остаётся данными; здесь — как по нему идти. Обратно
импортировать нельзя: stages берёт из pipeline контракт выходов, и обход внутри него замкнул бы
цикл.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from app.answers import Answers, answers_file
from app.clarify import Clarify, has_questions
from app.config import settings
from app.dialog import Turn
from app.ingest import Source, build_transcript, child_transcript
from app.pipeline import (
    ANSWERS,
    ASSIGNMENT_JSON,
    BRIEF_QUESTION,
    CANDIDATES,
    CLARIFY_JSON,
    ISSUES_JSON,
    PROJECT,
    RESEARCH,
    RESEARCH_SKIPPED,
    REVIEW_JSON,
    STEPS_JSON,
    TRANSCRIPT,
    PauseKind,
    Stage,
    StopKind,
    stages_between,
)
from app.project import (
    ProjectContextError,
    ProjectContextMissing,
    ProjectContextTooLarge,
    Standards,
    configured_directory,
    project_snapshot,
    read_standards,
    refreshed_snapshot,
)
from app.publish import publish, publish_task
from app.review import Review
from app.stages import (
    StageError,
    StageResult,
    dialog_history,
    load_template,
    previous_answer,
    run_stage,
)
from app.steps import assignment_of, meeting_lang_of
from app.transcribe import transcribe

logger = logging.getLogger(__name__)


class Run(BaseModel):
    root: Path
    run_id: str
    # Язык входа. У голосового его назовёт Whisper, и ingest перепишет поле распознанным.
    lang: str
    text: str = ""
    audio: Path | None = None
    # Откуда прогон, знает он сам, а не наличие записи: у продолженного записи на диске уже нет
    # (её мог унести `KEEP_AUDIO=false`), а подписан прогресс по-прежнему по источнику.
    source: Source
    # Согласие участников чужой записи (CLAUDE.md §8). Факт строки прогона, а не кнопки: у
    # продолженного прогона его приносит `Stopped`.
    consent_confirmed: bool | None = None
    # Ворота — норма, быстрый режим — исключение (CLAUDE.md §1), поэтому снимает их тот, кто заводит
    # прогон, и делает это явно.
    auto_approve: bool = False
    # Есть ли кому отвечать на вопросы стадии (SPEC §3.3). Умолчание — «некому»: у локального
    # прогона интерфейса ответов нет вовсе, и вопрос там встал бы навсегда (CLAUDE.md §1).
    interactive: bool = False
    # Разбор, из которого прогон взял работу (P3-08, фаза 2): его каталог, номер и поручение в
    # нём. У бота каталог это runs/<родитель>, у локального прогона текущий каталог.
    parent_root: Path | None = None
    parent_run_id: str | None = None
    assignment: int | None = None
    # Показывать ли владельцу вопросы для тимлида и ждать ответа (P3-11). Умолчание «нет»: у
    # локального прогона и без ворот ждать некому, и вопросы уходят на карточку открытыми.
    asks_teamlead: bool = False
    # Ответ тимлида или отказ его ждать: приносит тот, кто продолжает припаркованный прогон.
    answers: Answers | None = None


class Pause(BaseModel):
    """Остановка обхода: чего ждёт прогон и от кого.

    `choice` — человек выбирает одну идею из нескольких, подтверждать там нечего.
    `gate` — человек подтверждает готовый артефакт; `auto_approve` снимает только эти остановки.
    `answer` — человек отвечает на вопрос брифа (§3.3); подтверждать там тоже нечего, но и
    случиться она может только у прогона, которому есть кому отвечать (`Run.interactive`).
    `questions` — вопросы для тимлида отданы владельцу (P3-11): это не остановка чата, прогон
    ждёт ответа днями и только у того, кто спрашивает тимлида (`Run.asks_teamlead`).
    """

    stage: str
    artifact: str
    kind: PauseKind


class Redo(BaseModel):
    """Повтор стадии с ответом человека: чем править и что стадия отдала в прошлый раз.

    Зеркало `Pause`: остановка выходит из обхода, `Redo` заходит обратно в него, и род у них
    общий — на сорвавшемся повторе он восстанавливает ту же остановку.

    `turns` — прежние ходы диалога брифа; текущий вопрос в них не входит, он лежит артефактом.
    `closes_branch` — ветка выхода, которую этот повтор стадии больше не позволяет
    (`outputs_after`): род остановки на неё не отвечает, потому что один и тот же ответ на
    вопрос брифа то кончает диалог, то нет.
    """

    kind: StopKind
    user_edit: str
    artifact: str
    turns: tuple[Turn, ...] = ()
    closes_branch: str | None = None


class ConsentMissing(RuntimeError):
    """Файл с диктофона без согласия участников: запись не обрабатывается (CLAUDE.md §8)."""


def ingest_body(run: Run) -> dict[str, str]:
    # Кнопка согласия живёт в боте, но `auto_approve` снимает всё, кроме consent (SPEC §3.2), и
    # стадия проверяет его сама — до ffmpeg и до первого байта в OpenAI.
    if run.source == "file" and run.consent_confirmed is not True:
        raise ConsentMissing(f"Прогон {run.run_id}: файл без подтверждённого согласия на запись")
    if run.audio is None:
        return {
            TRANSCRIPT: build_transcript(
                run.text, run.lang, run.run_id, run.source, None, run.consent_confirmed
            )
        }
    heard = transcribe(run.audio, run.run_id)
    # Язык прогона задаёт голос, а не DEFAULT_LANG: brief и стадии за ним читают уже его.
    run.lang = heard.lang
    return {
        TRANSCRIPT: build_transcript(
            heard.text,
            heard.lang,
            run.run_id,
            run.source,
            heard.duration_seconds,
            run.consent_confirmed,
        )
    }


def handoff_body(run: Run) -> dict[str, str]:
    if run.parent_root is None or run.parent_run_id is None:
        raise ValueError(f"Прогон {run.run_id} не знает разбора, из которого взять расшифровку")
    parent = read_artifact(run.parent_root, TRANSCRIPT)
    return {TRANSCRIPT: child_transcript(parent, run.run_id, run.parent_run_id)}


def research_body(run: Run) -> dict[str, str]:
    # Настоящий ресёрч, положенный руками или прошлым прогоном, затирать нечем.
    if (run.root / RESEARCH).exists():
        return {}
    return {RESEARCH: RESEARCH_SKIPPED}


def publish_body(run: Run) -> dict[str, str]:
    publish(run.root / ISSUES_JSON)
    return {}


def current_standards() -> Standards | None:
    """Стандарты из PROJECT_CONTEXT_DIR; None, если настройка пуста. Ошибки каталога летят выше."""
    directory = configured_directory()
    return read_standards(directory) if directory else None


def log_snapshot(run: Run, stage: str, standards: Standards | None) -> None:
    if standards is None:
        logger.info("stage=%s run=%s project_context=unset", stage, run.run_id)
        return
    logger.info(
        "stage=%s run=%s project_context=%s files=%s chars=%d",
        stage,
        run.run_id,
        "snapshot" if standards.texts else "empty",
        ",".join([*standards.texts, *standards.same_as]),
        sum(len(text) for text in standards.texts.values()),
    )


def assignment_body(run: Run) -> dict[str, str]:
    if run.parent_root is None or run.parent_run_id is None or run.assignment is None:
        raise ValueError(f"Прогон {run.run_id} не знает разбора, из которого взять поручение")
    review = Review.model_validate_json(read_artifact(run.parent_root, REVIEW_JSON))
    built = assignment_of(
        review,
        run.assignment,
        run.run_id,
        run.parent_run_id,
        meeting_lang_of(run.source, run.lang, review.meeting_lang),
    )
    # Заданный, но сломанный каталог роняет прогон до вызова модели: владелец рассчитывает на
    # стандарты, и тихий прогон без них потратил бы деньги на шаги не по его стеку.
    try:
        standards = current_standards()
    except ProjectContextError as error:
        raise StageError(str(error), "") from error
    log_snapshot(run, "assignment", standards)
    return {
        ASSIGNMENT_JSON: built.model_dump_json(indent=2) + "\n",
        PROJECT: project_snapshot(standards, datetime.now(UTC)),
    }


def answers_body(run: Run) -> dict[str, str]:
    clarify = Clarify.model_validate_json(read_artifact(run.root, CLARIFY_JSON))
    if not has_questions(clarify):
        answers = Answers(status="nothing_asked")
    elif not run.asks_teamlead:
        answers = Answers(status="not_sent")
    elif run.answers is None:
        raise ValueError(f"Прогон {run.run_id} продолжен после вопросов, но без ответа тимлида")
    else:
        answers = run.answers
    fresh: Standards | ProjectContextMissing | ProjectContextTooLarge | None
    try:
        fresh = current_standards()
    except (ProjectContextMissing, ProjectContextTooLarge) as error:
        fresh = error
    snapshot, kept = refreshed_snapshot(
        read_artifact(run.root, PROJECT), fresh, datetime.now(UTC)
    )
    if kept is not None:
        logger.warning("stage=answers run=%s project_context=kept reason=%s", run.run_id, kept)
    elif not isinstance(fresh, ProjectContextError):
        log_snapshot(run, "answers", fresh)
    return {ANSWERS: answers_file(answers), PROJECT: snapshot}


def card_body(run: Run) -> dict[str, str]:
    publish_task(run.root / STEPS_JSON)
    return {}


# Тело стадии-кода: отдаёт файлы к записи и вправе поправить сам прогон — ingest так проставляет
# язык, распознанный Whisper. Другого места у языка нет: он свойство прогона, а не артефакта.
BODIES: dict[str, Callable[[Run], dict[str, str]]] = {
    "ingest": ingest_body,
    "handoff": handoff_body,
    "research": research_body,
    "publish": publish_body,
    "assignment": assignment_body,
    "answers": answers_body,
    "card": card_body,
}


def write_artifact(root: Path, path: str, content: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    logger.info("Записан %s", target)


def read_artifact(root: Path, path: str) -> str:
    return (root / path).read_text(encoding="utf-8")


def missing_before(root: Path, start: str, stop: str) -> list[str]:
    """Артефакты, которых обход не создаст сам, а прочитать попробует.

    Проверять только входы стартовой стадии мало: следующие читают то, что пропущенные должны
    были положить, и прогон падал бы посреди работы, успев записать часть файлов.
    """
    written: set[str] = set()
    missing = []
    for stage in stages_between(start, stop):
        missing += [
            path for path in stage.inputs if path not in written and not (root / path).exists()
        ]
        # Объединение по всем наборам выходов: intake отдаёт либо idea.md, либо candidates.md, но
        # ветка с кандидатами останавливает обход, и стадия, читающая idea.md, до неё не доходит.
        written |= set().union(*stage.outputs)
    return missing


def outputs_after(stage: Stage, redo: Redo | None) -> tuple[frozenset[str], ...]:
    """Что стадии позволено отдать: повтор вправе закрыть ветку, которая его и вызвала.

    Человек выбрал одну идею — значит, отдать список снова стадия не вправе: прогон встал бы на
    том же месте с тем же вопросом, а ответ человека пропал бы. Тем же запретом кончается диалог
    брифа: после «Собирай» и после последнего вопроса из бюджета стадия обязана собрать бриф.
    На воротах и на обычном ответе закрывать нечего: там правят и спрашивают дальше.
    """
    if redo is None or redo.closes_branch is None:
        return stage.outputs
    return tuple(paths for paths in stage.outputs if redo.closes_branch not in paths)


def run_llm_stage(run: Run, stage: Stage, redo: Redo | None = None) -> StageResult:
    inputs = {path: read_artifact(run.root, path) for path in stage.inputs}
    if stage.template:
        inputs[f"templates/{stage.template}"] = load_template(stage.template)
    params = dict(stage.params)
    if stage.needs_lang:
        params["lang"] = run.lang
    if stage.needs_interactive:
        params["interactive"] = "true" if run.interactive else "false"
    if stage.needs_owner_lang:
        params["owner_lang"] = settings.owner_lang
    history = (
        dialog_history(redo.turns)
        + previous_answer(redo.artifact, read_artifact(run.root, redo.artifact))
        if redo
        else None
    )
    try:
        return run_stage(
            stage.name,
            inputs,
            run.run_id,
            user_edit=redo.user_edit if redo else None,
            history=history,
            params=params,
            allowed=outputs_after(stage, redo),
        )
    except StageError as error:
        write_artifact(run.root, f"outputs/{stage.name}.raw.md", error.raw)
        raise


def walk(
    run: Run,
    start: str,
    stop: str,
    on_done: Callable[[Stage], None] = lambda stage: None,
    redo: Redo | None = None,
) -> Pause | None:
    """Идёт по списку от start до stop включительно. Отдаёт остановку, если прогон ждёт человека.

    `redo` — правка человека для стартовой стадии: обход возвращается в ту же стадию, с которой
    встал, и дальше идёт обычным порядком. Следующим стадиям правка не достаётся: она была про
    артефакт стартовой.
    """
    for stage in stages_between(start, stop):
        if stage.runs == "code":
            files = BODIES[stage.name](run)
        else:
            files = run_llm_stage(run, stage, redo if stage.name == start else None).files
        for path, content in files.items():
            write_artifact(run.root, path, content)
        on_done(stage)
        if CANDIDATES in files:
            return Pause(stage=stage.name, artifact=CANDIDATES, kind="choice")
        if BRIEF_QUESTION in files:
            return Pause(stage=stage.name, artifact=BRIEF_QUESTION, kind="answer")
        if (
            CLARIFY_JSON in files
            and run.asks_teamlead
            and has_questions(Clarify.model_validate_json(files[CLARIFY_JSON]))
        ):
            return Pause(stage=stage.name, artifact=CLARIFY_JSON, kind="questions")
        # Ворота останавливают прогон перед следующей стадией, а после stop останавливать нечего:
        # обход и так закончился, и «ждёт человека» вместо «дошёл до конца» соврало бы.
        if stage.gate_after and not run.auto_approve and stage.name != stop:
            return Pause(stage=stage.name, artifact=stage.gate_after, kind="gate")
    return None
