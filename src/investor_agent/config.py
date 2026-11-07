"""Settings loaded from .env and environment variables. Secrets are never logged."""

import os
from pathlib import Path

from pydantic import BaseModel, field_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseModel):
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model_fast: str = "deepseek-flash"
    deepseek_model_pro: str = "deepseek-v4-pro"

    telegram_bot_token: str = ""
    telegram_chat_id: str = "@investor_duker"
    telegram_admin_chat_id: str = ""
    publish_mode: str = "auto"

    fred_api_key: str = ""
    database_path: Path = Path("data/investor.sqlite3")
    prompts_dir: Path = PROJECT_ROOT / "prompts"
    radar_config_path: Path = PROJECT_ROOT / "config" / "radar.yaml"
    scheduler_enabled: bool = True

    @field_validator("publish_mode")
    @classmethod
    def _mode(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"auto", "draft"}:
            raise ValueError("PUBLISH_MODE must be 'auto' or 'draft'")
        return value

    def __repr__(self) -> str:  # never print secrets
        return f"Settings(publish_mode={self.publish_mode!r}, chat={self.telegram_chat_id!r})"

    __str__ = __repr__


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip().upper()] = value
    return values


ALIASES = {"TELEGRAM_PUBLISH_MODE": "PUBLISH_MODE", "DEEPSEEK_MODEL": None}


def load_settings(env_file: Path | str = ".env") -> Settings:
    merged = _parse_env_file(Path(env_file))
    merged.update({k.upper(): v for k, v in os.environ.items()})
    for old, new in ALIASES.items():
        if old in merged and new and new not in merged:
            merged[new] = merged[old]
    fields = Settings.model_fields
    data = {name: merged[name.upper()] for name in fields if name.upper() in merged and merged[name.upper()] != ""}
    if "scheduler_enabled" in data:
        data["scheduler_enabled"] = str(data["scheduler_enabled"]).lower() in {"1", "true", "yes"}
    return Settings(**data)
