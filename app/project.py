"""Стандарты рабочего проекта и их снимок inputs/project.md. См. SPEC.md §4, §8 (P3-11).

Каталог и файлы ищутся по правилам загрузчика Rigorous (`load-context.mjs`), чтобы бот видел ровно
то, что видит `/rigorous` в том же репозитории. Полного пути нет ни в снимке, ни в ошибках: только
имя последнего каталога.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

import frontmatter
from pydantic import BaseModel

from app.config import settings

StandardName = Literal["STACK.md", "PRINCIPLES.md", "TESTING.md"]

# Порядок значим: текст файла, на который смотрят несколько имён, ложится под первое. STACK.md
# первым, потому что он решает режим ресёрча.
STANDARD_ALIASES: dict[StandardName, tuple[str, ...]] = {
    "STACK.md": ("STACK.md", "TECH_STACK.md"),
    "PRINCIPLES.md": ("PRINCIPLES.md", "ENGINEERING.md", "CODE_STANDARDS.md"),
    "TESTING.md": ("TESTING.md", "TEST_STRATEGY.md"),
}
SEARCHED_SUBDIRECTORIES = ("", ".agents/context", "docs")

# Около 15 тыс. токенов на каждой из трёх стадий, что читают снимок. Обрезать нельзя: молча
# срезанный STACK.md потерял бы раздел отвергнутого.
MAX_STANDARDS_CHARACTERS = 60_000

HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
TODO_MARKER = re.compile(r"\[TODO\]", re.IGNORECASE)
STANDARD_BLOCK = re.compile(r'<standard name="([^"]+)">\n(.*?)\n</standard>', re.DOTALL)

KeptReason = Literal["missing", "too_large", "empty"]


class ProjectContextError(Exception):
    pass


class ProjectContextMissing(ProjectContextError):
    def __init__(self, source: str) -> None:
        super().__init__(
            f"Каталог стандартов проекта {source} не найден: поправьте PROJECT_CONTEXT_DIR "
            "или очистите его"
        )


class ProjectContextTooLarge(ProjectContextError):
    def __init__(self) -> None:
        super().__init__(
            "Стандарты проекта длиннее 60 000 знаков: сократите файлы или укажите каталог "
            "с короткой версией"
        )


class Standards(BaseModel):
    source: str
    # Текст лежит под первым из имён, что смотрят на один файл; остальные имена в `same_as`.
    texts: dict[StandardName, str]
    same_as: dict[StandardName, StandardName]


class Project(BaseModel):
    configured: bool
    source: str | None
    taken_at: datetime
    stack: bool
    principles: bool
    testing: bool
    same_as: dict[StandardName, StandardName]
    # Каждое найденное имя со своим текстом, `same_as` уже развёрнут.
    texts: dict[StandardName, str]


def configured_directory() -> Path | None:
    raw = settings.project_context_dir.strip()
    return Path(raw).expanduser() if raw else None


def is_placeholder(content: str) -> bool:
    """Правило заглушки загрузчика Rigorous: такой файл считается отсутствующим."""
    stripped = HTML_COMMENT.sub("", content).strip()
    return len(stripped) < 100 or (bool(TODO_MARKER.search(stripped)) and len(stripped) < 400)


def standard_file(directory: Path, name: StandardName) -> Path | None:
    """Первый существующий файл по синонимам, как есть, в нижнем и верхнем регистре.

    Заглушка под первым именем не уступает место синониму: загрузчик Rigorous тоже берёт её.
    """
    for alias in STANDARD_ALIASES[name]:
        for variant in (alias, alias.lower(), alias.upper()):
            candidate = directory / variant
            if candidate.is_file():
                return candidate
    return None


def find_standards_dir(directory: Path) -> Path | None:
    """Сам каталог, затем `.agents/context`, затем `docs`: первый, где есть хоть один стандарт.

    Загрузчик Rigorous выбирает первый с PRINCIPLES.md. Боту важнее STACK.md, и каталог со
    STACK.md без PRINCIPLES.md загрузчик не выбрал бы вовсе, а бот его видит.
    """
    for subdirectory in SEARCHED_SUBDIRECTORIES:
        candidate = directory / subdirectory if subdirectory else directory
        if any(standard_file(candidate, name) for name in STANDARD_ALIASES):
            return candidate
    return None


def read_standards(directory: Path) -> Standards:
    source = directory.absolute().name
    if not directory.is_dir():
        raise ProjectContextMissing(source)
    found_in = find_standards_dir(directory)
    texts: dict[StandardName, str] = {}
    same_as: dict[StandardName, StandardName] = {}
    first_name_of: dict[Path, StandardName] = {}
    if found_in is not None:
        for name in STANDARD_ALIASES:
            path = standard_file(found_in, name)
            if path is None:
                continue
            content = path.read_text(encoding="utf-8")
            if is_placeholder(content):
                continue
            # В `.agents/context` все три имени бывают ссылками на один файл: текст один раз.
            resolved = path.resolve()
            if resolved in first_name_of:
                same_as[name] = first_name_of[resolved]
                continue
            first_name_of[resolved] = name
            texts[name] = content
    characters = sum(len(text) for text in texts.values())
    if characters > MAX_STANDARDS_CHARACTERS:
        raise ProjectContextTooLarge()
    return Standards(source=source, texts=texts, same_as=same_as)


def present(standards: Standards | None, name: StandardName) -> bool:
    return standards is not None and (name in standards.texts or name in standards.same_as)


def project_snapshot(standards: Standards | None, taken_at: datetime) -> str:
    """Снимок стандартов; `None` значит, что PROJECT_CONTEXT_DIR не задан.

    Строки в кавычках JSON: имя каталога из одних цифр YAML прочитал бы числом, как было с run_id.
    """
    header = [
        f"configured: {json.dumps(standards is not None)}",
        f"source: {json.dumps(standards.source if standards else None, ensure_ascii=False)}",
        f"taken_at: {json.dumps(taken_at.isoformat())}",
        f"stack: {json.dumps(present(standards, 'STACK.md'))}",
        f"principles: {json.dumps(present(standards, 'PRINCIPLES.md'))}",
        f"testing: {json.dumps(present(standards, 'TESTING.md'))}",
        f"same_as: {json.dumps(standards.same_as if standards else {})}",
    ]
    # Тег <standard>, а не <file>: <file> занят выходом стадий, и вход не должен выглядеть
    # образцом ответа.
    blocks = [
        f'<standard name="{name}">\n{text.rstrip()}\n</standard>'
        for name, text in (standards.texts.items() if standards else ())
    ]
    return "---\n" + "\n".join(header) + "\n---\n" + "".join(f"{block}\n" for block in blocks)


def read_project(content: str) -> Project:
    post = frontmatter.loads(content)
    project = Project.model_validate({**post.metadata, "texts": {}})
    own_texts = dict(STANDARD_BLOCK.findall(post.content))
    for name in STANDARD_ALIASES:
        if name in own_texts:
            project.texts[name] = own_texts[name]
        elif name in project.same_as:
            project.texts[name] = own_texts[project.same_as[name]]
    return project


def refreshed_snapshot(
    previous: str,
    fresh: Standards | ProjectContextMissing | ProjectContextTooLarge | None,
    taken_at: datetime,
) -> tuple[str, KeptReason | None]:
    """Снимок для стадии answers: свежий, если стандарты читаются, иначе прежний и причина.

    Между вопросами и ответом проходят дни. Ответ тимлида уже получен, и сорвать прогон из-за
    отмонтированного каталога хуже, чем идти по снимку двухдневной давности. Каталог, опустевший
    после полного снимка, тоже не снимает стандартов молча: осознанно это делают «Стоп» и новое
    нажатие.
    """
    if isinstance(fresh, ProjectContextMissing):
        return previous, "missing"
    if isinstance(fresh, ProjectContextTooLarge):
        return previous, "too_large"
    if fresh is not None and not fresh.texts and read_project(previous).texts:
        return previous, "empty"
    return project_snapshot(fresh, taken_at), None
