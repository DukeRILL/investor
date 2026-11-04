from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = "@investor_duker"
    telegram_publish_mode: str = "draft"
    telegram_parse_mode: str = "HTML"
    deepseek_api_key: str = Field(default="")
    deepseek_model: str = "deepseek-chat"
    deepseek_base_url: str = "https://api.deepseek.com"
    agent_api_token: str = Field(default="")
    database_path: Path = Path("data/investor_agent.sqlite3")

    @field_validator("telegram_publish_mode")
    @classmethod
    def validate_publish_mode(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in {"draft", "auto"}:
            raise ValueError("TELEGRAM_PUBLISH_MODE must be 'draft' or 'auto'")
        return normalized
