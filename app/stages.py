"""LLM-стадии: промпт из .claude/commands/<stage>.md, вызов Anthropic, файлы из ответа.

См. SPEC.md §7.
"""

import json
import logging
import re
import time
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import TypeVar, cast

import anthropic
from anthropic import DefaultHttpxClient, Omit, omit
from anthropic.types import (
    ContentBlockParam,
    Message,
    MessageParam,
    ThinkingConfigParam,
    Usage,
    WebSearchTool20260318Param,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.answers import read_answers
from app.approach import (
    Approach,
    check_approach,
    leaked_in_queries,
    stamp_approach,
    unverified_problems,
    unverified_sources,
)
from app.candidates import check_candidates
from app.clarify import Clarify, check_clarify, stamp_clarify
from app.config import LiveApiNotAllowed, MissingApiKey, settings
from app.dialog import Turn, check_question
from app.models import IssuesFile
from app.pipeline import (
    ANSWERS,
    APPROACH_JSON,
    APPROACH_MD,
    ASSIGNMENT_JSON,
    BRIEF_QUESTION,
    CANDIDATES,
    CLARIFY_JSON,
    ISSUES_JSON,
    ISSUES_MD,
    PROJECT,
    REVIEW_JSON,
    REVIEW_MD,
    STEPS_JSON,
    STEPS_MD,
    TRANSCRIPT,
    stage_named,
)
from app.project import read_project
from app.render import approach_markdown, issues_markdown, review_markdown, steps_markdown
from app.review import (
    Review,
    check_review,
    fragments,
    stamp_review,
    unmatched_originals,
    unmatched_tickets,
)
from app.steps import Assignment, Pair, Steps, check_steps, stamp_steps
from app.validate import check_issues

logger = logging.getLogger(__name__)

Parsed = TypeVar("Parsed")

ROOT = Path(__file__).resolve().parent.parent
COMMANDS_DIR = ROOT / ".claude" / "commands"
API_MODE_PROMPT = ROOT / "templates" / "api_mode.md"

# Без явного таймаута SDK считает выход по 28 токенов в секунду и запрещает нестримовый запрос
# уже на 21 334 токенах. Замеренная скорость стадий — около 110 в секунду, то есть потолок
# в 24 000 укладывается примерно в четыре минуты; десять — запас на медленный ответ.
REQUEST_TIMEOUT_SECONDS = 600.0

FILE_BLOCK = re.compile(r"""<file\s+path=["']([^"']+)["']\s*>\n?(.*?)</file>""", re.DOTALL)

# Серверный цикл поиска API прерывает паузой (`stop_reason: pause_turn`), и пауза может
# повториться; предел продолжений ставит вызывающий (SPEC §7).
MAX_SEARCH_CONTINUATIONS = 3

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
    if stage == "clarify":
        assignment = Assignment.model_validate_json(inputs[ASSIGNMENT_JSON])
        return check_clarify(files[CLARIFY_JSON], assignment)
    if stage == "approach":
        return check_approach(files[APPROACH_JSON])
    if stage == "steps":
        assignment = Assignment.model_validate_json(inputs[ASSIGNMENT_JSON])
        asked = Clarify.model_validate_json(inputs[CLARIFY_JSON]).questions
        found = Approach.model_validate_json(inputs[APPROACH_JSON]).new_questions
        return check_steps(files[STEPS_JSON], assignment, len(asked) + len(found))
    if CANDIDATES in files:
        return check_candidates(files[CANDIDATES])
    if BRIEF_QUESTION in files:
        return check_question(files[BRIEF_QUESTION])
    return []


def unmatched_problems(files: dict[str, str], transcript: str) -> list[str]:
    """Дословные фрагменты и тикеты разбора, которых нет в расшифровке. Претензия первого ответа.

    После ремонта несошедшийся фрагмент стадию не роняет, а получает пометку (SPEC §7): на
    встрече их десятки, и одно склеенное моделью составное слово отнимало бы весь разбор. Ключ
    и ссылка тикета после ремонта тоже не роняют, а обнуляются штампом.
    """
    try:
        review = Review.model_validate_json(files[REVIEW_JSON])
    except ValidationError:
        # Сломанную форму уже назвал check_review: сверять фрагменты не в чем.
        return []
    return [
        f"в расшифровке дословно нет фрагмента «{original}»: скопируй его из расшифровки как есть"
        for original in unmatched_originals(review, transcript)
    ] + [
        f"в тексте нет ключа или ссылки тикета «{identifier}»: скопируй их из текста буква в "
        "букву или поставь null, ссылку из ключа не собирай"
        for identifier in unmatched_tickets(review, transcript)
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


def log_query_leaks(run_id: str, queries: list[str], assignment: Assignment) -> None:
    for leaked in leaked_in_queries(queries, assignment.task):
        logger.warning("stage=approach run=%s query_has=%s", run_id, leaked)


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


def thinking_of(stage: str) -> ThinkingConfigParam | Omit:
    """Размышление стадии словами API. Без записи параметр не отправляется вовсе (SPEC §7)."""
    mode = stage_named(stage).thinking
    if mode is None:
        return omit
    return {"type": "adaptive"} if mode == "adaptive" else {"type": "disabled"}


def thinking_tokens(usage: Usage) -> str:
    """Сколько токенов выхода ушло на размышление, если API это сказал. Иначе прочерк."""
    details = usage.output_tokens_details
    return str(details.thinking_tokens) if details is not None else "-"


def text_length(response: Message) -> int:
    return sum(len(block.text) for block in response.content if block.type == "text")


def search_tools(stage: str, inputs: dict[str, str]) -> list[WebSearchTool20260318Param] | Omit:
    """Инструмент поиска стадии; предел поисков — по флагу `stack` снимка стандартов её входа.

    Кто зовёт поиск, объявляет запись стадии: у нас прямой вызов, а не включённая по умолчанию
    версии динамическая фильтрация, которая отбирает результаты кодом в песочнице (SPEC §7).
    """
    budget = stage_named(stage).web_search
    if budget is None:
        return omit
    stack = read_project(inputs[PROJECT]).stack
    return [
        {
            "type": "web_search_20260318",
            "name": "web_search",
            "max_uses": budget.max_uses_with_stack if stack else budget.max_uses_without_stack,
            "allowed_callers": list(budget.callers),
        }
    ]


def search_result_urls(response: Message) -> set[str]:
    """URL из результатов поиска и из цитат ответа: всё, что модель в этом ходу видела.

    Прямой режим блоков с `caller` не рождает, но обход их переживает смену режима и ничего не
    стоит: они приходят такими же блоками верхнего уровня, отдельного обхода вложенным не нужно.
    """
    urls: set[str] = set()
    for block in response.content:
        if block.type == "web_search_tool_result" and isinstance(block.content, list):
            urls |= {result.url for result in block.content}
        if block.type == "text" and block.citations:
            urls |= {
                citation.url
                for citation in block.citations
                if citation.type == "web_search_result_location"
            }
    return urls


def results_in_context(response: Message) -> int:
    """Сколько результатов поиска легло в контекст модели.

    Считаются только результаты прямого вызова: у блока с `caller` кода они уходят
    фильтрующему коду в песочницу, а до модели не доходят — ровно так дефект живой проверки
    части E и остался незаметным (SPEC §7). Ошибка поиска приходит объектом вместо списка.
    """
    return sum(
        len(block.content)
        for block in response.content
        if block.type == "web_search_tool_result"
        and isinstance(block.content, list)
        and (block.caller is None or block.caller.type == "direct")
    )


def citations_made(response: Message) -> int:
    """Сколько раз текст ответа сослался на найденную страницу."""
    return sum(
        sum(1 for citation in block.citations if citation.type == "web_search_result_location")
        for block in response.content
        if block.type == "text" and block.citations
    )


def search_queries(response: Message) -> list[str]:
    """Запросы, ушедшие в поиск. Владелец должен видеть, что именно ушло наружу (SPEC §8)."""
    return [
        query
        for block in response.content
        if block.type == "server_tool_use" and block.name == "web_search"
        if isinstance(query := block.input.get("query"), str)
    ]


def searches_made(usage: Usage) -> int:
    return usage.server_tool_use.web_search_requests if usage.server_tool_use else 0


def assistant_blocks(response: Message) -> MessageParam:
    """Ответ ассистента блоками, слово в слово.

    Так его требует вернуть продолжение паузы: в блоках результатов лежит `encrypted_content`,
    и изменённый блок API отвергает (400).
    """
    return {"role": "assistant", "content": cast("list[ContentBlockParam]", response.content)}


def answered_turn(stage: str, response: Message, raw: str) -> MessageParam:
    """Прошлый ответ стадии для истории ремонтного повтора.

    Стадия с поиском отдаёт блоки как есть, по той же причине. Остальным довольно склеенного
    текста, и он же лежал там до P3-11.
    """
    if stage_named(stage).web_search is None:
        return {"role": "assistant", "content": raw}
    return assistant_blocks(response)


class ModelTurn(BaseModel):
    """Ход стадии целиком: последний ответ, приостановленные до него и счёт по всему ходу."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    response: Message
    # Ответы, которыми API прерывал серверный цикл поиска: в истории они идут перед последним.
    paused: list[Message] = Field(default_factory=list)
    input_tokens: int
    output_tokens: int
    duration_ms: int
    urls: set[str] = Field(default_factory=set)
    queries: list[str] = Field(default_factory=list)
    # Результаты, дошедшие до контекста модели, и цитаты на них: по этой паре видно, что поиск
    # был не только оплачен, но и прочитан (SPEC §7).
    results: int = 0
    citations: int = 0


def ask_once(
    stage: str,
    run_id: str,
    model: str,
    messages: list[MessageParam],
    tools: list[WebSearchTool20260318Param] | Omit,
) -> tuple[Message, int]:
    started = time.perf_counter()
    try:
        response = anthropic_client().messages.create(
            model=model,
            max_tokens=settings.anthropic_max_tokens,
            system=load_prompt(stage) + "\n\n" + API_MODE_PROMPT.read_text(encoding="utf-8"),
            messages=messages,
            thinking=thinking_of(stage),
            tools=tools,
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
    # Размышление тарифицируется как выход и по умолчанию идёт у модели само (SPEC §7), а в
    # ответе виден только его блок, часто с пустым текстом. Без этих трёх полей разницу между
    # «модель много написала» и «модель долго думала» в логе не увидеть.
    logger.info(
        "stage=%s run=%s model=%s input_tokens=%d output_tokens=%d thinking=%s "
        "thinking_tokens=%s text_chars=%d duration_ms=%d web_search_requests=%d",
        stage,
        run_id,
        response.model,
        response.usage.input_tokens,
        response.usage.output_tokens,
        "yes" if any(block.type == "thinking" for block in response.content) else "no",
        thinking_tokens(response.usage),
        text_length(response),
        duration_ms,
        searches_made(response.usage),
    )
    for query in search_queries(response):
        logger.info("stage=%s run=%s query=%s", stage, run_id, query)
    return response, duration_ms


def ask_model(
    stage: str,
    run_id: str,
    model: str,
    messages: list[MessageParam],
    tools: list[WebSearchTool20260318Param] | Omit,
) -> ModelTurn:
    """Ход стадии вместе с продолжениями серверного цикла поиска (SPEC §7).

    `pause_turn` значит, что API прервал долгий цикл поиска, а не что ход кончился: продолжение
    это тот же запрос с теми же инструментами и с ответом ассистента, отданным блоками без
    единой правки. Пауза может повториться сколько угодно раз, и предел ставим мы.
    """
    paused: list[Message] = []
    input_tokens = output_tokens = duration_ms = results = citations = 0
    urls: set[str] = set()
    queries: list[str] = []
    while True:
        history = [*messages, *(assistant_blocks(answer) for answer in paused)]
        response, step_ms = ask_once(stage, run_id, model, history, tools)
        input_tokens += response.usage.input_tokens
        output_tokens += response.usage.output_tokens
        duration_ms += step_ms
        urls |= search_result_urls(response)
        queries += search_queries(response)
        results += results_in_context(response)
        citations += citations_made(response)
        if response.stop_reason != "pause_turn":
            return ModelTurn(
                response=response,
                paused=paused,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_ms=duration_ms,
                urls=urls,
                queries=queries,
                results=results,
                citations=citations,
            )
        if len(paused) == MAX_SEARCH_CONTINUATIONS:
            raise StageError(
                f"{stage}: серверный цикл поиска не кончился за {MAX_SEARCH_CONTINUATIONS} "
                "продолжения",
                "",
            )
        paused.append(response)


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


def parsed_input(
    stage: str, inputs: dict[str, str], path: str, parse: Callable[[str], Parsed]
) -> Parsed:
    """Вход стадии поручения по его схеме, до вызова модели.

    Входы пишет код, но их можно поправить руками, а вход с чужой формой проверить и
    проштамповать нечем: платить за такой вызов незачем.
    """
    try:
        return parse(inputs[path])
    except ValueError as error:
        raise StageError(f"{stage}: {path} не проходит схему: {error}", "") from error


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
    if stage in ("clarify", "approach", "steps"):
        assignment = parsed_input(stage, inputs, ASSIGNMENT_JSON, Assignment.model_validate_json)
        project = parsed_input(stage, inputs, PROJECT, read_project)
    if stage == "approach":
        answers = parsed_input(stage, inputs, ANSWERS, read_answers)
        # Поручение и ответ тимлида одним текстом: и названный стек, и библиотека варианта
        # ищутся в обоих, и разводить два поиска по двум строкам нечем.
        assignment_and_answers = f"{inputs[ASSIGNMENT_JSON]}\n{answers.text}"
    if stage == "steps":
        clarify = parsed_input(stage, inputs, CLARIFY_JSON, Clarify.model_validate_json)
        answers = parsed_input(stage, inputs, ANSWERS, read_answers)
        researched = parsed_input(stage, inputs, APPROACH_JSON, Approach.model_validate_json)
    messages: list[MessageParam] = [
        *(history or []),
        {"role": "user", "content": build_user_message(inputs, user_edit, params)},
    ]
    tools = search_tools(stage, inputs)
    turn = ask_model(stage, run_id, model, messages, tools)
    response = turn.response
    if stage == "approach":
        log_query_leaks(run_id, turn.queries, assignment)
    raw = answer_text(stage, response)
    files = without_extras(stage, parse_file_blocks(raw))
    input_tokens = turn.input_tokens
    output_tokens = turn.output_tokens
    duration_ms = turn.duration_ms
    searched = turn.urls
    queries = turn.queries
    results = turn.results
    citations = turn.citations

    problems = repairable_problems(stage, files, outputs, inputs, given_params)
    if stage == "review" and frozenset(files) in outputs:
        problems += unmatched_problems(files, inputs[TRANSCRIPT])
    if stage == "approach" and frozenset(files) in outputs:
        problems += unverified_problems(
            files[APPROACH_JSON], searched, assignment, assignment_and_answers, project
        )
    if problems:
        # Удачный ремонт стирал причину: прогон выглядел как два вызова без объяснения,
        # а претензии оставались только у провалившихся.
        logger.warning(
            "stage=%s run=%s repair=1 problems=%s", stage, run_id, "; ".join(problems)
        )
        repair = ask_model(
            stage,
            run_id,
            model,
            [
                *messages,
                *(assistant_blocks(answer) for answer in turn.paused),
                answered_turn(stage, response, raw),
                {"role": "user", "content": repair_request(problems)},
            ],
            tools,
        )
        duration_ms += repair.duration_ms
        input_tokens += repair.input_tokens
        output_tokens += repair.output_tokens
        searched |= repair.urls
        queries += repair.queries
        results += repair.results
        citations += repair.citations
        response = repair.response
        if stage == "approach":
            log_query_leaks(run_id, repair.queries, assignment)
        raw = answer_text(stage, response)
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
    if stage == "clarify":
        files[CLARIFY_JSON] = stamp_clarify(files[CLARIFY_JSON], assignment)
    if stage == "approach":
        files[APPROACH_JSON] = stamp_approach(
            files[APPROACH_JSON], assignment, assignment_and_answers, project, searched, queries
        )
        approach = Approach.model_validate_json(files[APPROACH_JSON])
        # По этой строке живые прогоны считают, как часто модель придумывает ссылку. Оплаченный
        # поиск без результатов и без цитат — это ресёрч по памяти, и он идёт предупреждением:
        # живая проверка части E три часа выглядела удачной именно потому, что этой пары в
        # строке не было (SPEC §7).
        blind = bool(approach.searches) and not (results and citations)
        logger.log(
            logging.WARNING if blind else logging.INFO,
            "stage=approach run=%s mode=%s searches=%d results=%d citations=%d "
            "sources=%d unverified=%d",
            run_id,
            approach.mode,
            len(approach.searches),
            results,
            citations,
            len(approach.sources),
            unverified_sources(approach),
        )
        files[APPROACH_MD] = approach_markdown(approach, project, assignment_and_answers)
    if stage == "steps":
        files[STEPS_JSON] = stamp_steps(
            files[STEPS_JSON],
            assignment,
            clarify.title,
            [Pair(text=asked.text, translation=asked.translation) for asked in clarify.questions],
            [
                Pair(text=found.text, translation=found.translation)
                for found in researched.new_questions
            ],
            answers.status == "answered",
        )
        files[STEPS_MD] = steps_markdown(
            Steps.model_validate_json(files[STEPS_JSON]), answers, project
        )
    return StageResult(
        files=files,
        model=response.model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_ms=duration_ms,
    )
