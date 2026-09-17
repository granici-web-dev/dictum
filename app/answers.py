"""inputs/answers.md: ответ тимлида на вопросы поручения, как его вставил владелец. См. SPEC.md §5.

Файл пишется всегда, одним из четырёх состояний: «ответа нет» заявлено данными, а не отсутствием
файла, и входы стадий остаются обязательными. Какие вопросы ответ закрывает, решает не код, а
стадия шагов по содержанию: владелец отправляет не все вопросы, тимлид отвечает не на все и
нумерует по-своему.
"""

import json
from datetime import datetime
from typing import Literal, Self

import frontmatter
from pydantic import BaseModel, model_validator

# answered: ответ пришёл; without_answers: владелец решил идти без него; not_sent: вопросы были,
# но прогон их не показывал и не ждал; nothing_asked: спрашивать было нечего.
AnswersStatus = Literal["answered", "without_answers", "not_sent", "nothing_asked"]


class Answers(BaseModel):
    status: AnswersStatus
    received_at: datetime | None = None
    text: str = ""

    @model_validator(mode="after")
    def only_an_answer_has_a_time_and_a_text(self) -> Self:
        answered = self.status == "answered"
        if answered != (self.received_at is not None) or answered != bool(self.text.strip()):
            raise ValueError(
                "received_at и текст есть у answered и только у него, "
                f"а у {self.status} received_at={self.received_at}, текст {len(self.text)} знаков"
            )
        return self


def answers_file(answers: Answers) -> str:
    header = [f"status: {answers.status}"]
    if answers.received_at is not None:
        header.append(f"received_at: {json.dumps(answers.received_at.isoformat())}")
    body = f"{answers.text.strip()}\n" if answers.text.strip() else ""
    return "---\n" + "\n".join(header) + "\n---\n" + body


def read_answers(content: str) -> Answers:
    post = frontmatter.loads(content)
    return Answers.model_validate({**post.metadata, "text": post.content})
