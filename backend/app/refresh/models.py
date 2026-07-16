from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta


MARKET_CLOSE_VERIFICATION_TIME = time(18, 0)
REFRESH_WINDOW_LABEL = "24 小時分流排程 Asia/Taipei"
CLOSE_VERIFICATION_JOB_NAME = "market_close_verification"
RETRY_BACKOFF_SECONDS = (60, 180, 300, 900)
STALE_PE_RETRY_INTERVAL = timedelta(minutes=15)
MARKET_OPEN_TIME = time(9, 0)
MARKET_CLOSE_TIME = time(13, 30)
HISTORY_REFRESH_TIME = time(18, 5)
BROKER_REFRESH_TIME = time(18, 10)
FUNDAMENTAL_REFRESH_TIME = time(18, 20)
SCHEDULER_TICK_SECONDS = 5

CHANNEL_QUOTE = "QUOTE"
CHANNEL_FUNDAMENTALS = "FUNDAMENTALS"
CHANNEL_BROKER = "BROKER"
CHANNEL_HISTORY = "HISTORY"
REFRESH_CHANNELS = (CHANNEL_QUOTE, CHANNEL_FUNDAMENTALS, CHANNEL_BROKER, CHANNEL_HISTORY)
DEFAULT_CHANNEL_TIMEOUT_SECONDS = {
    CHANNEL_QUOTE: 60.0,
    CHANNEL_FUNDAMENTALS: 180.0,
    CHANNEL_BROKER: 90.0,
    CHANNEL_HISTORY: 180.0,
}
CHANNEL_CATEGORIES = {
    CHANNEL_QUOTE: ("QUOTE",),
    CHANNEL_FUNDAMENTALS: ("CURRENT_PE", "EPS", "FINANCIAL_QUARTER", "MONTHLY_REVENUE"),
    CHANNEL_BROKER: ("BROKER_TRADING",),
    CHANNEL_HISTORY: ("TECHNICAL_DAILY", "PE_HISTORY"),
}

PRIORITY_MANUAL = 0
PRIORITY_RETRY = 10
PRIORITY_AUTO = 20


@dataclass
class RefreshSymbolState:
    symbol: str
    status: str
    message: str = ""
    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True)
class RefreshJob:
    symbol: str
    channel: str = CHANNEL_QUOTE
    categories: frozenset[str] = field(default_factory=lambda: frozenset(("QUOTE",)))
    priority: int = PRIORITY_AUTO
    force_full: bool = False
    profile_required: bool = False


@dataclass
class ChannelRuntime:
    current_symbols: set[str] = field(default_factory=set)
    last_finished_at: datetime | None = None
    next_run_at: datetime | None = None
