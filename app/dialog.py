"""Контракт brief-диалога: ход разговора и бюджет вопросов. См. SPEC.md §3.3.

Вопрос пишет модель, а показывает человеку бот, поэтому форма проверяется на выходе стадии —
тем же порядком, что и candidates.md в app/candidates.py.
"""

from pydantic import BaseModel

from app.config import settings

# Вопрос идёт человеку сообщением, а не файлом, и сообщение Telegram обрывается на 4096
# символах. Это не правило формы, а страховка: сама форма («одно предложение») стоит в
# /brief, а вопрос на три тысячи знаков — уже не вопрос, а бриф, и спрашивать его нечего.
MAX_QUESTION_CHARACTERS = 3000


class Turn(BaseModel):
    """Ход диалога: вопрос стадии и ответ человека на него.

    Копится в `runs.brief_dialog` (SPEC §4): ответы человека не лежат ни в одном артефакте, а
    стадии нужны все сразу, и сообщение в чате прогон не переживёт.
    """

    question: str
    answer: str


def check_question(text: str) -> list[str]:
    problems = []
    if not text.strip():
        problems.append("вопрос пустой: человеку ушло бы сообщение без единого слова")
    if len(text) > MAX_QUESTION_CHARACTERS:
        problems.append(
            f"вопрос длиннее {MAX_QUESTION_CHARACTERS} символов, столько Telegram не отправит: "
            "нужно одно предложение"
        )
    return problems


def budget_spent(asked: int) -> bool:
    """Вопросов задано столько, сколько прогону положено: дальше стадия собирает бриф.

    Число живёт в настройках, а не в коде: каждый вопрос — вызов модели и ожидание человека.
    """
    return asked >= settings.max_brief_questions
