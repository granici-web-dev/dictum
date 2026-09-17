"""LLM-стадии: промпт из .claude/commands/<stage>.md, вызов Anthropic, файлы из ответа.

См. SPEC.md §7.
"""

import json
import logging
import re
import time
from functools import cache
from pathlib import Path

import anthropic
from anthropic import DefaultHttpxClient
from anthropic.types import Message, MessageParam
from pydantic import BaseModel, ValidationError

from app.candidates import check_candidates
from app.config import LiveApiNotAllowed, MissingApiKey, settings
from app.dialog import Turn, check_question
from app.models import IssuesFile
from app.pipeline import (
    BRIEF_QUESTION,
    CANDIDATES,
    ISSUES_JSON,
    ISSUES_MD,
    REVIEW_JSON,
    REVIEW_MD,
    TRANSCRIPT,
    stage_named,
)
from app.render import issues_markdown, review_markdown
from app.review import Review, check_review, fragments, stamp_review, unmatched_originals
from app.validate import check_issues

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
COMMANDS_DIR = ROOT / ".claude" / "commands"
API_MODE_PROMPT = ROOT / "templates" / "api_mode.md"

# Без явного таймаута SDK считает выход по 28 токенов в секунду и запрещает нестримовый запрос
# уже на 21 334 токенах. Замеренная скорость стадий — около 110 в секунду, то есть потолок
# в 24 000 укладывается примерно в четыре минуты; десять — запас на медленный ответ.
REQUEST_TIMEOUT_SECONDS = 600.0

FILE_BLOCK = re.compile(r"""<file\s+path=["']([^"']+)["']\s*>\n?(.*?)</file>""", re.DOTALL)

NO_FILE_BLOCKS = 'в ответе нет ни одного тега <file path="...">'


def closed_branch(got: frozenset[str], allowed: tuple[frozenset[str], ...]) -> str:
    listed = " или ".join(", ".join(sorted(paths)) for paths in allowed)
    return (
        f"стадия отдала {', '.join(sorted(got))}, а после правки человека допустим "
        f"только {listed}"
    )


def repairable_problems(
    stage: str,
    files: dict[str, str],
    allowed: tuple[frozenset[str], ...],
    inputs: dict[str, str],
    params: dict[str, str],
) -> list[str]:
    if not files:
        return [NO_FILE_BLOCKS]
    given = frozenset(files)
    # Чужой набор файлов ремонту не подлежит, и run_stage обязан упасть на нём раньше,
    # чем на претензиях: иначе порядок двух проверок в конце run_stage перестанет быть верным.
    if given not in stage_named(stage).outputs:
        return []
    # Набор знакомый, но повтор его уже не принимает: человек выбрал идею, а стадия отдала
    # второй список кандидатов. Это ремонтируется — стадия видит свой прошлый ответ и правку.
    if given not in allowed:
        return [closed_branch(given, allowed)]
    if stage == "decompose":
        return check_issues(files[ISSUES_JSON])
    if stage == "review":
        return check_review(files[REVIEW_JSON], inputs[TRANSCRIPT], params["owner_lang"])
    if CANDIDATES in files:
        return check_candidates(files[CANDIDATES])
    if BRIEF_QUESTION in files:
        return check_question(files[BRIEF_QUESTION])
    return []


def unmatched_problems(files: dict[str, str], transcript: str) -> list[str]:
    """Дословные фрагменты разбора, которых нет в расшифровке. Претензия только первого ответа.

    После ремонта несошедшийся фрагмент стадию не роняет, а получает пометку (SPEC §7): на
    встрече их десятки, и одно склеенное моделью составное слово отнимало бы весь разбор.
    """
    try:
        review = Review.model_validate_json(files[REVIEW_JSON])
    except ValidationError:
        # Сломанную форму уже назвал check_review: сверять фрагменты не в чем.
        return []
    return [
        f"в расшифровке дословно нет фрагмента «{original}»: скопируй его из расшифровки как есть"
        for original in unmatched_originals(review, transcript)
    ]


def without_extras(stage: str, files: dict[str, str]) -> dict[str, str]:
    """Выбрасывает лишний файл при полном ожидаемом наборе. Недостающий — по-прежнему ошибка.

    Живой прогон 2ebcf8868ca3dbf9 умер на том, что decompose прислал вместе с issues.json ещё и
    issues.md, хотя промпт этого прямо не велит: половина из 9585 токенов ушла на копию, которую
    код всё равно рисует сам из JSON, и прогон встал на проверке набора. Копия ничего не решает
    и ремонтного повтора не стоит — она выбрасывается, а запись в логе даёт чем посчитать дрейф
    промпта. Файла, которого стадия ждёт, это не касается: он так и остаётся ошибкой.
    """
    given = frozenset(files)
    for wanted in stage_named(stage).outputs:
        extra = given - wanted
        if wanted <= given and extra:
            logger.warning(
                "stage=%s extra=%s", stage, ", ".join(sorted(extra))
            )
            return {path: files[path] for path in sorted(wanted)}
    return files


def previous_answer(path: str, content: str) -> list[MessageParam]:
    """Прошлый ответ стадии для истории повтора, собранный из артефакта.

    Сырой текст ответа прогон не переживает (SPEC §7.3), а файл переживает, и стадии нужен
    именно он: без своего прошлого ответа она соберёт артефакт заново.
    """
    return [{"role": "assistant", "content": f'<file path="{path}">\n{content}</file>'}]


def dialog_history(turns: tuple[Turn, ...]) -> list[MessageParam]:
    """Прежние ходы диалога брифа: вопрос стадии, ответ человека, и так по кругу (SPEC §3.3).

    Вопрос, на который отвечают сейчас, сюда не входит: он лежит артефактом на диске, и историю
    замыкает `previous_answer` — тем же способом, что и у любого другого повтора.
    """
    messages: list[MessageParam] = []
    for turn in turns:
        messages += previous_answer(BRIEF_QUESTION, turn.question)
        messages.append({"role": "user", "content": f"<user_edit>\n{turn.answer}\n</user_edit>"})
    return messages


def repair_request(problems: list[str]) -> str:
    listed = "\n".join(f"- {problem}" for problem in problems)
    return (
        f"Предыдущий ответ не прошёл проверку:\n{listed}\n\n"
        "Исправь перечисленное и верни результат целиком в тегах <file path=\"...\">. "
        "Меняй только то, на что указано: остальное должно остаться слово в слово прежним."
    )


class StageResult(BaseModel):
    files: dict[str, str]
    model: str
    input_tokens: int
    output_tokens: int
    duration_ms: int


class StageError(Exception):
    def __init__(self, message: str, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


def load_prompt(stage: str) -> str:
    return (COMMANDS_DIR / f"{stage_named(stage).name}.md").read_text(encoding="utf-8")


def load_template(name: str) -> str:
    return (ROOT / "templates" / name).read_text(encoding="utf-8")


def http_client() -> DefaultHttpxClient:
    return DefaultHttpxClient()


@cache
def anthropic_client() -> anthropic.Anthropic:
    if not settings.allow_live_api:
        raise LiveApiNotAllowed(
            "ALLOW_LIVE_API is not true, nothing was sent. "
            "Set ALLOW_LIVE_API=true in .env for a run you mean to pay for."
        )
    if not settings.anthropic_api_key:
        raise MissingApiKey(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in."
        )
    return anthropic.Anthropic(
        api_key=settings.anthropic_api_key,
        max_retries=2,
        timeout=REQUEST_TIMEOUT_SECONDS,
        http_client=http_client(),
    )


def build_user_message(
    inputs: dict[str, str],
    user_edit: str | None,
    params: dict[str, str] | None,
) -> str:
    parts = []
    if params:
        rendered = "\n".join(f"{name}: {value}" for name, value in params.items())
        parts.append(f"<params>\n{rendered}\n</params>")
    parts += [f'<file path="{path}">\n{content}\n</file>' for path, content in inputs.items()]
    if user_edit:
        parts.append(f"<user_edit>\n{user_edit}\n</user_edit>")
    return "\n\n".join(parts)


def parse_file_blocks(text: str) -> dict[str, str]:
    files: dict[str, str] = {}
    for path, body in FILE_BLOCK.findall(text):
        if path in files:
            raise StageError(f"two <file> blocks share the path {path}", text)
        files[path] = body.strip("\r\n") + "\n"
    return files


def ask_model(
    stage: str, run_id: str, model: str, messages: list[MessageParam]
) -> tuple[Message, int]:
    started = time.perf_counter()
    try:
        response = anthropic_client().messages.create(
            model=model,
            max_tokens=settings.anthropic_max_tokens,
            system=load_prompt(stage) + "\n\n" + API_MODE_PROMPT.read_text(encoding="utf-8"),
            messages=messages,
        )
    except anthropic.APIError as error:
        logger.warning(
            "stage=%s run=%s model=%s duration_ms=%d error=%s",
            stage,
            run_id,
            model,
            int((time.perf_counter() - started) * 1000),
            type(error).__name__,
        )
        raise
    duration_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "stage=%s run=%s model=%s input_tokens=%d output_tokens=%d duration_ms=%d",
        stage,
        run_id,
        response.model,
        response.usage.input_tokens,
        response.usage.output_tokens,
        duration_ms,
    )
    return response, duration_ms


def answer_text(stage: str, response: Message) -> str:
    raw = "".join(block.text for block in response.content if block.type == "text")
    if response.stop_reason != "end_turn":
        hint = " Raise ANTHROPIC_MAX_TOKENS." if response.stop_reason == "max_tokens" else ""
        raise StageError(f"{stage}: model stopped with {response.stop_reason}.{hint}", raw)
    return raw


def with_run_id(issues_json: str, run_id: str) -> str:
    data = json.loads(issues_json)
    data["run_id"] = run_id
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def run_stage(
    stage: str,
    inputs: dict[str, str],
    run_id: str,
    user_edit: str | None = None,
    history: list[MessageParam] | None = None,
    params: dict[str, str] | None = None,
    allowed: tuple[frozenset[str], ...] | None = None,
) -> StageResult:
    """`allowed` сужает выходы стадии: повтор по правке не принимает ветку, которая его вызвала."""
    outputs = allowed if allowed is not None else stage_named(stage).outputs
    given_params = params or {}
    model = settings.anthropic_model_decompose if stage == "decompose" else settings.anthropic_model
    messages: list[MessageParam] = [
        *(history or []),
        {"role": "user", "content": build_user_message(inputs, user_edit, params)},
    ]
    response, duration_ms = ask_model(stage, run_id, model, messages)
    raw = answer_text(stage, response)
    files = without_extras(stage, parse_file_blocks(raw))
    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens

    problems = repairable_problems(stage, files, outputs, inputs, given_params)
    if stage == "review" and frozenset(files) in outputs:
        problems += unmatched_problems(files, inputs[TRANSCRIPT])
    if problems:
        # Удачный ремонт стирал причину: прогон выглядел как два вызова без объяснения,
        # а претензии оставались только у провалившихся.
        logger.warning(
            "stage=%s run=%s repair=1 problems=%s", stage, run_id, "; ".join(problems)
        )
        repair, repair_ms = ask_model(
            stage,
            run_id,
            model,
            [
                *messages,
                {"role": "assistant", "content": raw},
                {"role": "user", "content": repair_request(problems)},
            ],
        )
        duration_ms += repair_ms
        input_tokens += repair.usage.input_tokens
        output_tokens += repair.usage.output_tokens
        response = repair
        raw = answer_text(stage, repair)
        files = without_extras(stage, parse_file_blocks(raw))
        problems = repairable_problems(stage, files, outputs, inputs, given_params)

    if frozenset(files) not in outputs:
        expected = " or ".join(", ".join(sorted(paths)) for paths in outputs)
        got = ", ".join(sorted(files)) or "no <file> blocks"
        raise StageError(f"{stage}: expected {expected}, got {got}", raw)
    if problems:
        listed = "\n".join(f"- {problem}" for problem in problems)
        raise StageError(f"{stage}: ответ не прошёл проверку и после повтора:\n{listed}", raw)
    if stage == "decompose":
        # В issues.json run_id вписывают здесь, чтобы publish его только читал.
        files[ISSUES_JSON] = with_run_id(files[ISSUES_JSON], run_id)
        files[ISSUES_MD] = issues_markdown(IssuesFile.model_validate_json(files[ISSUES_JSON]))
    if stage == "review":
        files[REVIEW_JSON] = stamp_review(
            files[REVIEW_JSON], inputs[TRANSCRIPT], given_params["owner_lang"]
        )
        review = Review.model_validate_json(files[REVIEW_JSON])
        unverified = sum(not fragment.in_transcript for fragment in fragments(review))
        logger.info("stage=review run=%s unverified=%d", run_id, unverified)
        files[REVIEW_MD] = review_markdown(review)
    return StageResult(
        files=files,
        model=response.model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_ms=duration_ms,
    )
