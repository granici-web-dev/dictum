"""Настройки из .env и ошибки конфигурации, общие для всех исходящих клиентов."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    anthropic_model_decompose: str = "claude-sonnet-5"
    anthropic_max_tokens: int = 24000
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


class ConfigError(RuntimeError):
    pass


class MissingApiKey(ConfigError):
    pass


class LiveApiNotAllowed(ConfigError):
    pass


class InvalidProjectKey(ConfigError):
    pass


settings = Settings()
