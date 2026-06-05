from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "stock_valuation.sqlite3"


def _load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _load_environment() -> None:
    _load_dotenv_file(PROJECT_ROOT / ".env")
    _load_dotenv_file(PROJECT_ROOT / "backend" / ".env")


def _parse_origins(value: str) -> list[str]:
    return [origin.strip() for origin in value.split(",") if origin.strip()]


@dataclass(frozen=True)
class Settings:
    app_env: str
    database_url: str
    cors_origins: list[str]
    wantgoo_base_url: str
    background_refresh_seconds: int
    crawler_log_retention_days: int
    crawler_log_cleanup_interval_hours: int
    api_version: str = "0.1.0"


def _parse_positive_int(value: str, default: int) -> int:
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def get_settings() -> Settings:
    _load_environment()

    return Settings(
        app_env=os.getenv("APP_ENV", "development"),
        database_url=os.getenv(
            "DATABASE_URL",
            "sqlite:///./data/stock_valuation.sqlite3",
        ),
        cors_origins=_parse_origins(
            os.getenv(
                "CORS_ORIGINS",
                "http://localhost:5173,http://127.0.0.1:5173,http://localhost:5174,http://127.0.0.1:5174",
            )
        ),
        wantgoo_base_url=os.getenv("WANTGOO_BASE_URL", "https://www.wantgoo.com"),
        background_refresh_seconds=_parse_positive_int(
            os.getenv("BACKGROUND_REFRESH_SECONDS", "60"),
            60,
        ),
        crawler_log_retention_days=_parse_positive_int(
            os.getenv("CRAWLER_LOG_RETENTION_DAYS", "30"),
            30,
        ),
        crawler_log_cleanup_interval_hours=_parse_positive_int(
            os.getenv("CRAWLER_LOG_CLEANUP_INTERVAL_HOURS", "24"),
            24,
        ),
    )
