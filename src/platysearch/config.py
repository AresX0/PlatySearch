"""Application-wide configuration loaded from environment / .env."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    db_path: Path = Path("data/platysearch.db")
    crawl_delay: float = 0.2
    crawl_concurrency: int = 5
    max_pages: int = 10_000
    ai_penalty: float = 0.6
    host: str = "0.0.0.0"
    port: int = 8000
    user_agent: str = "PlatySearchBot/0.1 (+https://github.com/platysearch)"

    # Scheduler settings
    scheduler_hour: int = 3
    scheduler_minute: int = 0
    scheduler_timezone: str = "America/Chicago"
    nightly_crawl_pages: int = 2000
    max_db_size_mb: int = 4000
    crawl_enabled: bool = True

    model_config = {"env_prefix": "PLATY_", "env_file": ".env", "extra": "ignore"}


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
