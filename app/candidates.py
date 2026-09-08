"""Контракт outputs/candidates.md: исход intake и форма списка идей. См. SPEC.md §3.2.

Файл пишет модель, а показывает человеку бот, поэтому форма проверяется на выходе стадии —
так же, как issues.json в app/validate.py.
"""

import re
from typing import Literal

import frontmatter
from pydantic import BaseModel

Outcome = Literal["multiple", "none"]

OUTCOMES = ("multiple", "none")

# Номер с нулевой колонки и название в жирном. Разбирать выход модели можно только потому, что
# форма объявлена контрактом в /intake: без якоря сюда попадали вложенные подпункты и перенос
# прозы вроде «2024. год был тяжёлым».
IDEA_LINE = re.compile(r"^(\d+)\. \*\*(.+?)\*\*", re.MULTILINE)

# Строка списка без разбора названия: по разнице с IDEA_LINE видно идею, у которой название не
# выделено. Без этой проверки такая строка просто не доехала бы до человека, и он выбирал бы из
# двух идей, зная одну.
NUMBERED_LINE = re.compile(r"^(\d+)\. ", re.MULTILINE)


class Idea(BaseModel):
    number: int
    title: str


class Candidates(BaseModel):
    """Что intake положил в файл: какой это случай и что показать человеку.

    `multiple` — идей несколько, `none` — задания в записи нет, и список тогда перечисляет
    темы разговора. Различать их по заголовку нельзя: он на языке прогона.
    """

    outcome: Outcome
    ideas: list[Idea]


def ideas_in(body: str) -> list[Idea]:
    return [Idea(number=int(number), title=title) for number, title in IDEA_LINE.findall(body)]


def parse_candidates(text: str) -> Candidates:
    post = frontmatter.loads(text)
    return Candidates.model_validate(
        {"outcome": post.metadata.get("outcome"), "ideas": ideas_in(post.content)}
    )


def check_candidates(text: str) -> list[str]:
    post = frontmatter.loads(text)
    problems = []
    outcome = post.metadata.get("outcome")
    if outcome not in OUTCOMES:
        problems.append(f"outcome: {outcome!r} — во frontmatter нужно multiple или none")
    numbered = [int(number) for number in NUMBERED_LINE.findall(post.content)]
    named = [idea.number for idea in ideas_in(post.content)]
    # Список обязателен только при multiple: выбирать надо из чего-то. При none обсуждения
    # могло не быть вовсе, и строка ради формы — выдуманные данные (CLAUDE.md §5): на живом
    # трёхсекундном голосовом модель дописала тему «Пустая запись», которой в записи не было.
    if not numbered and outcome != "none":
        problems.append("нет ни одной строки вида `N. **Название** — …` с начала строки")
    unnamed = [number for number in numbered if number not in named]
    if unnamed:
        problems.append(
            "название не выделено **жирным** в строках: "
            + ", ".join(str(number) for number in unnamed)
        )
    if numbered != list(range(1, len(numbered) + 1)):
        problems.append("номера идут не подряд с 1: " + ", ".join(str(n) for n in numbered))
    return problems
