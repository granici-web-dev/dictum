"""LLM-стадии. Промпт каждой стадии читается из .claude/commands/<stage>.md.

Контракт: run_stage(name, inputs, user_edit) -> str (содержимое артефакта).
См. SPEC.md §7. Реализация — задача P1-02.
"""

from pathlib import Path

COMMANDS_DIR = Path(__file__).resolve().parent.parent / ".claude" / "commands"
STAGES = ("intake", "brief", "research", "prd", "decompose")


def load_prompt(stage: str) -> str:
    if stage not in STAGES:
        raise ValueError(f"unknown stage: {stage}")
    return (COMMANDS_DIR / f"{stage}.md").read_text(encoding="utf-8")


def run_stage(stage: str, inputs: dict[str, str], user_edit: str | None = None) -> str:
    raise NotImplementedError("P1-02")
