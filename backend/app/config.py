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
    finmind_token: str | None
    ai_provider: str
    gemini_api_key: str | None
    gemini_model: str
    openrouter_api_key: str | None
    openrouter_model: str
    openrouter_fallback_models: list[str]
    openrouter_model_cooldown_seconds: int
    ai_rate_limit_cooldown_seconds: int
    ai_outage_cooldown_seconds: int
    ai_format_failure_cooldown_seconds: int
    background_refresh_seconds: int
    quote_market_interval_seconds: int
    quote_off_hours_interval_seconds: int
    pe_poll_interval_seconds: int
    monthly_revenue_release_interval_seconds: int
    futures_refresh_seconds: int
    crawler_log_retention_days: int
    crawler_log_cleanup_interval_hours: int
    database_backup_dir: str
    database_backup_retention_count: int
    database_backup_hour: int
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
        finmind_token=os.getenv("FINMIND_TOKEN") or None,
        ai_provider=os.getenv("AI_PROVIDER", "gemini").strip().lower(),
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY") or None,
        openrouter_model=os.getenv("OPENROUTER_MODEL", ""),
        openrouter_fallback_models=_parse_origins(os.getenv("OPENROUTER_FALLBACK_MODELS", "")),
        openrouter_model_cooldown_seconds=_parse_positive_int(
            os.getenv("OPENROUTER_MODEL_COOLDOWN_SECONDS", "600"),
            600,
        ),
        ai_rate_limit_cooldown_seconds=_parse_positive_int(
            os.getenv("AI_RATE_LIMIT_COOLDOWN_SECONDS", os.getenv("OPENROUTER_MODEL_COOLDOWN_SECONDS", "900")),
            900,
        ),
        ai_outage_cooldown_seconds=_parse_positive_int(
            os.getenv("AI_OUTAGE_COOLDOWN_SECONDS", "180"),
            180,
        ),
        ai_format_failure_cooldown_seconds=_parse_positive_int(
            os.getenv("AI_FORMAT_FAILURE_COOLDOWN_SECONDS", "1800"),
            1800,
        ),
        background_refresh_seconds=_parse_positive_int(
            os.getenv("BACKGROUND_REFRESH_SECONDS", "60"),
            60,
        ),
        quote_market_interval_seconds=_parse_positive_int(
            os.getenv("QUOTE_MARKET_INTERVAL_SECONDS", os.getenv("BACKGROUND_REFRESH_SECONDS", "60")),
            60,
        ),
        quote_off_hours_interval_seconds=_parse_positive_int(
            os.getenv("QUOTE_OFF_HOURS_INTERVAL_SECONDS", "900"),
            900,
        ),
        pe_poll_interval_seconds=_parse_positive_int(
            os.getenv("PE_POLL_INTERVAL_SECONDS", "900"),
            900,
        ),
        monthly_revenue_release_interval_seconds=_parse_positive_int(
            os.getenv("MONTHLY_REVENUE_RELEASE_INTERVAL_SECONDS", "7200"),
            7200,
        ),
        futures_refresh_seconds=_parse_positive_int(
            os.getenv("FUTURES_REFRESH_SECONDS", "10"),
            10,
        ),
        crawler_log_retention_days=_parse_positive_int(
            os.getenv("CRAWLER_LOG_RETENTION_DAYS", "30"),
            30,
        ),
        crawler_log_cleanup_interval_hours=_parse_positive_int(
            os.getenv("CRAWLER_LOG_CLEANUP_INTERVAL_HOURS", "24"),
            24,
        ),
        database_backup_dir=os.getenv("DATABASE_BACKUP_DIR", "./data/backups"),
        database_backup_retention_count=_parse_positive_int(
            os.getenv("DATABASE_BACKUP_RETENTION_COUNT", "14"),
            14,
        ),
        database_backup_hour=min(
            23,
            max(0, int(os.getenv("DATABASE_BACKUP_HOUR", "3")) if os.getenv("DATABASE_BACKUP_HOUR", "3").isdigit() else 3),
        ),
    )
