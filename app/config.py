"""Настройки из .env и ошибки конфигурации, общие для всех исходящих клиентов."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    anthropic_model_decompose: str = "claude-sonnet-5"
    anthropic_max_tokens: int = 16000
    allow_live_api: bool = False
    openai_api_key: str = ""
    telegram_bot_token: str = ""
    trello_key: str = ""
    trello_token: str = ""
    trello_board_id: str = ""
    project_key: str = ""
    database_url: str = "postgresql+psycopg://i2b:i2b@localhost:5432/i2b"
    redis_url: str = "redis://localhost:6379/0"
    default_lang: str = "de"
    keep_audio: bool = False


class ConfigError(RuntimeError):
    pass


class MissingApiKey(ConfigError):
    pass


class LiveApiNotAllowed(ConfigError):
    pass


class InvalidProjectKey(ConfigError):
    pass


settings = Settings()
