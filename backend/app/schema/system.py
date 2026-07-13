from datetime import date, datetime

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    app_env: str
    database: str
    api_version: str


class MetadataResponse(BaseModel):
    data_source: str
    api_version: str
    stocks_count: int
    valuations_count: int
    refresh_status: str | None = None
    refresh_interval_seconds: int | None = None
    auto_refresh_enabled: bool | None = None
    market_session: str | None = None
    refresh_window: str | None = None
    next_auto_refresh_at: datetime | None = None
    last_refresh_finished_at: datetime | None = None
    last_close_verification_at: datetime | None = None
    latest_official_data_date: date | None = None
