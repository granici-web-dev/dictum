"""LLM-стадии: промпт из .claude/commands/<stage>.md, вызов Anthropic, файлы из ответа. См. SPEC.md §7."""

import logging
import re
import time
from functools import cache
from pathlib import Path

import anthropic
from anthropic.types import MessageParam
from pydantic import BaseModel

from app.config import settings

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
COMMANDS_DIR = ROOT / ".claude" / "commands"
API_MODE_PROMPT = ROOT / "templates" / "api_mode.md"
STAGES = ("intake", "brief", "research", "prd", "decompose")

FILE_BLOCK = re.compile(r'<file path="([^"]+)">\n?(.*?)</file>', re.DOTALL)


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
    if stage not in STAGES:
        raise ValueError(f"unknown stage: {stage}")
    return (COMMANDS_DIR / f"{stage}.md").read_text(encoding="utf-8")


@cache
def anthropic_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=settings.anthropic_api_key or None, max_retries=2)


def build_system_prompt(stage: str) -> str:
    return load_prompt(stage) + "\n\n" + API_MODE_PROMPT.read_text(encoding="utf-8")


def build_user_message(inputs: dict[str, str], user_edit: str | None) -> str:
    parts = [f'<file path="{path}">\n{content}\n</file>' for path, content in inputs.items()]
    if user_edit:
        parts.append(f"<user_edit>\n{user_edit}\n</user_edit>")
    return "\n\n".join(parts)


def parse_file_blocks(text: str) -> dict[str, str]:
    return {path: body.strip() for path, body in FILE_BLOCK.findall(text)}


def run_stage(
    stage: str,
    inputs: dict[str, str],
    user_edit: str | None = None,
    history: list[MessageParam] | None = None,
) -> StageResult:
    model = settings.anthropic_model_decompose if stage == "decompose" else settings.anthropic_model
    messages: list[MessageParam] = [
        *(history or []),
        {"role": "user", "content": build_user_message(inputs, user_edit)},
    ]
    started = time.perf_counter()
    response = anthropic_client().messages.create(
        model=model,
        max_tokens=settings.anthropic_max_tokens,
        system=build_system_prompt(stage),
        messages=messages,
    )
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
        raise StageError(f"{stage}: model stopped with {response.stop_reason}", raw)
    files = parse_file_blocks(raw)
    if not files:
        raise StageError(f"{stage}: response has no <file> blocks", raw)
    return StageResult(
        files=files,
        model=response.model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        duration_ms=duration_ms,
    )
