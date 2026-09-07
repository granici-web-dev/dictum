"""Контракт issues.json: схема, уникальность id, граф зависимостей. См. SPEC.md §5.

python -m app.validate outputs/issues.json — проверка файла руками.
"""

import json
import sys
from collections import Counter
from pathlib import Path

from typing import Any

from pydantic import ValidationError

from app.models import IssuesFile


def find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    done: set[str] = set()
    path: list[str] = []

    def walk(node: str) -> list[str] | None:
        if node in path:
            return [*path[path.index(node) :], node]
        if node in done:
            return None
        path.append(node)
        for following in graph.get(node, []):
            cycle = walk(following)
            if cycle:
                return cycle
        path.pop()
        done.add(node)
        return None

    for node in graph:
        cycle = walk(node)
        if cycle:
            return cycle
    return None


def problem_location(parsed: Any, location: tuple[Any, ...]) -> str:
    path = ".".join(str(part) for part in location)
    if len(location) < 2 or location[0] != "issues":
        return path
    try:
        return ".".join([parsed["issues"][location[1]]["id"], *(str(p) for p in location[2:])])
    except (KeyError, IndexError, TypeError):
        return path


def check_issues(text: str) -> list[str]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        return [f"не разбирается как JSON: {error}"]
    try:
        issues_file = IssuesFile.model_validate(parsed)
    except ValidationError as error:
        return [
            f"{problem_location(parsed, problem['loc'])}: {problem['msg']}"
            for problem in error.errors()
        ]

    issues = issues_file.issues
    duplicates = [
        f"идентификатор {id_} встречается несколько раз"
        for id_, count in sorted(Counter(i.id for i in issues).items())
        if count > 1
    ]
    # Ниже граф строится по id как по ключу, а с дублями такой граф читается неоднозначно:
    # настоящий цикл на дублирующемся id потерялся бы молча.
    if duplicates:
        return duplicates

    declared_phases = {phase.n for phase in issues_file.phases}
    problems = [
        f"{i.id} стоит в фазе {i.phase}, которой нет в phases"
        for i in issues
        if i.phase not in declared_phases
    ]
    phase_of = {i.id: i.phase for i in issues}
    problems += [
        f"{i.id} зависит от {dependency}, которого нет"
        for i in issues
        for dependency in i.depends_on
        if dependency not in phase_of
    ]
    problems += [
        f"{i.id} из фазы {i.phase} зависит от {dependency} "
        f"из более поздней фазы {phase_of[dependency]}"
        for i in issues
        for dependency in i.depends_on
        if dependency in phase_of and phase_of[dependency] > i.phase
    ]
    cycle = find_cycle({i.id: i.depends_on for i in issues})
    if cycle:
        problems.append("цикл в зависимостях: " + " → ".join(cycle))
    return problems


def main(path: str) -> int:
    text = Path(path).read_text(encoding="utf-8")
    problems = check_issues(text)
    if problems:
        print(f"{path}: проблем {len(problems)}", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    parsed = json.loads(text)
    print(f"ok: {len(parsed['issues'])} issues, {len(parsed['phases'])} phases")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
