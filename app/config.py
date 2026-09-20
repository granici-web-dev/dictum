"""Настройки из .env и ошибки конфигурации, общие для всех исходящих клиентов."""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Сколько токенов claude-sonnet-5 отдаёт за один ответ (Models overview, сверено 20.09.2026).
# Больше этого API не даст и ответит 400 — уже после того, как расшифровка оплачена.
MODEL_MAX_TOKENS = 128000

# Ниже этого ответ стадии не помещается вместе с размышлением, которое у модели идёт по
# умолчанию: такой потолок обрывает не длинную запись, а любую.
LEAST_USEFUL_MAX_TOKENS = 1024


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    anthropic_model_decompose: str = "claude-sonnet-5"
    # Потолок одного ответа. 64 000 это половина потолка модели и запас 1,28 к ожидаемым
    # 50 000 токенов разбора самой длинной записи, которую система вообще принимает (138 минут,
    # один запрос Whisper). Поднимать есть куда: вторая половина оставлена нарочно.
    anthropic_max_tokens: int = 64000
    allow_live_api: bool = False
    openai_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_allowed_chat_ids: str = ""
    trello_key: str = ""
    trello_token: str = ""
    trello_board_id: str = ""
    # Доска пути идеи: фазы и по дюжине задач своей идеи не ложатся между рабочими колонками.
    # Старт бота её не требует: без неё полезен разбор встречи, а кнопка идеи отказывает сама.
    trello_idea_board_id: str = ""
    project_key: str = ""
    database_url: str = "postgresql+psycopg://dictum:dictum@localhost:5434/dictum"
    default_lang: str = "de"
    # Язык владельца установки, на нём пишется разбор встречи. Язык записи называет Whisper, и
    # живёт он в строке прогона; этот одинаков для всех прогонов (P3-08).
    owner_lang: str = "ru"
    keep_audio: bool = False
    # Ворота — норма, а не опция (CLAUDE.md §1): снимает их явный true в .env или /gates off, а не
    # умолчание.
    auto_approve: bool = False
    # Сколько вопросов брифу позволено задать за прогон. Каждый — отдельный вызов модели и
    # отдельное ожидание человека.
    max_brief_questions: int = 5
    # Каталог стандартов Rigorous рабочего проекта (P3-11). Пусто значит, что стандартов нет, а не
    # что их надо искать где-то по умолчанию: чужой каталог ушёл бы в Anthropic без ведома
    # владельца.
    project_context_dir: str = ""
    # Папка, куда владелец кладёт записи созвонов с ноутбука (SPEC §3.1). Пусто — поведения нет,
    # бот работает как сегодня. Задана, а каталога нет — бот не стартует: узнать о мимо набранном
    # пути, положив запись и прождав час, хуже, чем узнать на старте.
    meeting_inbox_dir: str = ""
    # Класть ли рядом с артефактами разбор ответов модели: типы блоков и usage по каждому вызову
    # (SPEC §7). По умолчанию нет: файл нужен диагностике, а не прогону.
    trace_stage_calls: bool = False

    @field_validator("anthropic_max_tokens")
    @classmethod
    def within_the_model(cls, asked: int) -> int:
        """Промах в .env ловится на старте, а не ответом API после оплаченной расшифровки.

        Тот же довод, по которому `ensure_schema()` стоит до Whisper (SPEC §3.1): узнать о
        промахе после $0.83 — худший момент из возможных.
        """
        if asked > MODEL_MAX_TOKENS:
            raise ValueError(
                f"ANTHROPIC_MAX_TOKENS={asked} больше потолка модели: claude-sonnet-5 отдаёт "
                f"не больше {MODEL_MAX_TOKENS} токенов за ответ. Поставьте значение не больше "
                f"{MODEL_MAX_TOKENS} в .env."
            )
        if asked < LEAST_USEFUL_MAX_TOKENS:
            raise ValueError(
                f"ANTHROPIC_MAX_TOKENS={asked} меньше {LEAST_USEFUL_MAX_TOKENS}: столько не "
                "хватит ни на один ответ стадии вместе с размышлением, и оборвётся не длинная "
                "запись, а любая."
            )
        return asked


class ConfigError(RuntimeError):
    pass


class MissingApiKey(ConfigError):
    pass


class LiveApiNotAllowed(ConfigError):
    pass


class InvalidProjectKey(ConfigError):
    pass


settings = Settings()
