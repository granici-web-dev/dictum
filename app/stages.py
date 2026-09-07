"""LLM-стадии: промпт из .claude/commands/<stage>.md, вызов Anthropic, файлы из ответа.

См. SPEC.md §7.
"""

import logging
import re
import time
from functools import cache
from pathlib import Path

import anthropic
from anthropic import DefaultHttpxClient
from anthropic.types import MessageParam
from pydantic import BaseModel

from app.config import settings

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
COMMANDS_DIR = ROOT / ".claude" / "commands"
API_MODE_PROMPT = ROOT / "templates" / "api_mode.md"
STAGES = ("intake", "brief", "research", "prd", "decompose")

STAGE_OUTPUTS: dict[str, tuple[frozenset[str], ...]] = {
    "intake": (frozenset({"inputs/idea.md"}), frozenset({"outputs/candidates.md"})),
    "brief": (frozenset({"outputs/brief.md"}),),
    "research": (frozenset({"outputs/research.md"}),),
    "prd": (frozenset({"outputs/prd.md"}),),
    "decompose": (frozenset({"outputs/issues.json", "outputs/issues.md"}),),
}

FILE_BLOCK = re.compile(r"""<file\s+path=["']([^"']+)["']\s*>\n?(.*?)</file>""", re.DOTALL)


class StageResult(BaseModel):
    files: dict[str, str]
    model: str
    input_tokens: int
    output_tokens: int
    duration_ms: int


class MissingApiKey(RuntimeError):
    pass


class StageError(Exception):
    def __init__(self, message: str, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


def load_prompt(stage: str) -> str:
    if stage not in STAGES:
        raise ValueError(f"unknown stage: {stage}")
    return (COMMANDS_DIR / f"{stage}.md").read_text(encoding="utf-8")


def http_client() -> DefaultHttpxClient:
    return DefaultHttpxClient()


@cache
def anthropic_client() -> anthropic.Anthropic:
    if not settings.anthropic_api_key:
        raise MissingApiKey(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in."
        )
    return anthropic.Anthropic(
        api_key=settings.anthropic_api_key,
        max_retries=2,
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


def run_stage(
    stage: str,
    inputs: dict[str, str],
    user_edit: str | None = None,
    history: list[MessageParam] | None = None,
    params: dict[str, str] | None = None,
) -> StageResult:
    model = settings.anthropic_model_decompose if stage == "decompose" else settings.anthropic_model
    messages: list[MessageParam] = [
        *(history or []),
        {"role": "user", "content": build_user_message(inputs, user_edit, params)},
    ]
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
            "stage=%s model=%s duration_ms=%d error=%s",
            stage,
            model,
            int((time.perf_counter() - started) * 1000),
            type(error).__name__,
        )
        raise
    duration_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "stage=%s model=%s input_tokens=%d output_tokens=%d duration_ms=%d",
        stage,
        response.model,
        response.usage.input_tokens,
        response.usage.output_tokens,
        duration_ms,
    )
    raw = "".join(block.text for block in response.content if block.type == "text")
    if response.stop_reason != "end_turn":
        hint = " Raise ANTHROPIC_MAX_TOKENS." if response.stop_reason == "max_tokens" else ""
        raise StageError(f"{stage}: model stopped with {response.stop_reason}.{hint}", raw)
    files = parse_file_blocks(raw)
    if frozenset(files) not in STAGE_OUTPUTS[stage]:
        expected = " or ".join(", ".join(sorted(paths)) for paths in STAGE_OUTPUTS[stage])
        got = ", ".join(sorted(files)) or "no <file> blocks"
        raise StageError(f"{stage}: expected {expected}, got {got}", raw)
    return StageResult(
        files=files,
        model=response.model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        duration_ms=duration_ms,
    )
