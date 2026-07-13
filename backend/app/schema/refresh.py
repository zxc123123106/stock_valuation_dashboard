from datetime import datetime

from pydantic import BaseModel, Field


class RefreshQueueResponse(BaseModel):
    status: str
    symbol: str | None = None
    symbols: list[str] = Field(default_factory=list)
    queued_at: datetime
    message: str


class RefreshSymbolStateResponse(BaseModel):
    symbol: str
    status: str
    message: str
    failure_count: int = 0
    last_error: str | None = None
    next_retry_at: datetime | None = None
    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RefreshChannelStatusResponse(BaseModel):
    status: str
    current_symbols: list[str] = Field(default_factory=list)
    queue_length: int = 0
    next_run_at: datetime | None = None
    last_finished_at: datetime | None = None


class RefreshStatusResponse(BaseModel):
    status: str
    current_symbol: str | None = None
    queue_length: int
    auto_refresh_enabled: bool = True
    market_session: str = "always_on"
    refresh_window: str = ""
    next_auto_refresh_at: datetime | None = None
    last_refresh_finished_at: datetime | None = None
    last_close_verification_at: datetime | None = None
    channels: dict[str, RefreshChannelStatusResponse] = Field(default_factory=dict)
    symbols: list[RefreshSymbolStateResponse]
