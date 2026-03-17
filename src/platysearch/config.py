"""Application-wide configuration loaded from environment / .env."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    db_path: Path = Path("data/platysearch.db")
    crawl_delay: float = 1.0
    max_pages: int = 10_000
    ai_penalty: float = 0.6
    host: str = "0.0.0.0"
    port: int = 8000
    user_agent: str = "PlatySearchBot/0.1 (+https://github.com/platysearch)"

    model_config = {"env_prefix": "PLATY_", "env_file": ".env", "extra": "ignore"}


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
