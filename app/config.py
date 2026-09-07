from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5"
    anthropic_model_decompose: str = "claude-sonnet-4-5"
    openai_api_key: str = ""
    telegram_bot_token: str = ""
    trello_key: str = ""
    trello_token: str = ""
    trello_board_id: str = ""
    database_url: str = "postgresql+psycopg://i2b:i2b@localhost:5432/i2b"
    redis_url: str = "redis://localhost:6379/0"
    default_lang: str = "de"
    keep_audio: bool = False


settings = Settings()
