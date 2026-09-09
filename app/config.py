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
    project_key: str = ""
    database_url: str = "postgresql+psycopg://dictum:dictum@localhost:5434/dictum"
    default_lang: str = "de"
    keep_audio: bool = False
    # Ворота — норма, а не опция (CLAUDE.md §1): снимает их стенд своим .env, а не умолчание.
    auto_approve: bool = False


class ConfigError(RuntimeError):
    pass


class MissingApiKey(ConfigError):
    pass


class LiveApiNotAllowed(ConfigError):
    pass


class InvalidProjectKey(ConfigError):
    pass


settings = Settings()
