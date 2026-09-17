"""Обход пайплайна: одна дорога для make run-text и для бота. См. SPEC.md §7.2.

Список стадий лежит в app/pipeline.py и остаётся данными; здесь — как по нему идти. Обратно
импортировать нельзя: stages берёт из pipeline контракт выходов, и обход внутри него замкнул бы
цикл.
"""

import logging
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from app.config import settings
from app.dialog import Turn
from app.ingest import Source, build_transcript
from app.pipeline import (
    BRIEF_QUESTION,
    CANDIDATES,
    ISSUES_JSON,
    RESEARCH,
    RESEARCH_SKIPPED,
    TRANSCRIPT,
    Stage,
    StopKind,
    stages_between,
)
from app.publish import publish
from app.stages import (
    StageError,
    StageResult,
    dialog_history,
    load_template,
    previous_answer,
    run_stage,
)
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


class Pause(BaseModel):
    """Остановка обхода: чего ждёт прогон и от кого.

    `choice` — человек выбирает одну идею из нескольких, подтверждать там нечего.
    `gate` — человек подтверждает готовый артефакт; `auto_approve` снимает только эти остановки.
    `answer` — человек отвечает на вопрос брифа (§3.3); подтверждать там тоже нечего, но и
    случиться она может только у прогона, которому есть кому отвечать (`Run.interactive`).
    """

    stage: str
    artifact: str
    kind: StopKind


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


def research_body(run: Run) -> dict[str, str]:
    # Настоящий ресёрч, положенный руками или прошлым прогоном, затирать нечем.
    if (run.root / RESEARCH).exists():
        return {}
    return {RESEARCH: RESEARCH_SKIPPED}


def publish_body(run: Run) -> dict[str, str]:
    publish(run.root / ISSUES_JSON)
    return {}


# Тело стадии-кода: отдаёт файлы к записи и вправе поправить сам прогон — ingest так проставляет
# язык, распознанный Whisper. Другого места у языка нет: он свойство прогона, а не артефакта.
BODIES: dict[str, Callable[[Run], dict[str, str]]] = {
    "ingest": ingest_body,
    "research": research_body,
    "publish": publish_body,
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
        # Ворота останавливают прогон перед следующей стадией, а после stop останавливать нечего:
        # обход и так закончился, и «ждёт человека» вместо «дошёл до конца» соврало бы.
        if stage.gate_after and not run.auto_approve and stage.name != stop:
            return Pause(stage=stage.name, artifact=stage.gate_after, kind="gate")
    return None
