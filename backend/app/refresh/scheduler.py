"""Pure time and market-session rules for the background scheduler."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from .models import MARKET_CLOSE_TIME, MARKET_CLOSE_VERIFICATION_TIME, MARKET_OPEN_TIME


TAIPEI_TZ = ZoneInfo("Asia/Taipei")


def as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_taipei(value: datetime) -> datetime:
    return as_aware_utc(value).astimezone(TAIPEI_TZ)


def stock_market_is_open(now: datetime) -> bool:
    local = to_taipei(now)
    return local.weekday() < 5 and MARKET_OPEN_TIME <= local.time() < MARKET_CLOSE_TIME


def previous_weekday(value: date) -> date:
    candidate = value - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def expected_official_trade_date(now: datetime) -> date:
    local = to_taipei(now)
    if local.weekday() < 5 and local.time() >= MARKET_CLOSE_VERIFICATION_TIME:
        return local.date()
    return previous_weekday(local.date())


def previous_month_period(now: datetime) -> str:
    local = to_taipei(now)
    year = local.year
    month = local.month - 1
    if month == 0:
        year -= 1
        month = 12
    return f"{year:04d}-{month:02d}"


def is_weekday(value: datetime) -> bool:
    return to_taipei(value).weekday() < 5


def auto_refresh_enabled(_now: datetime) -> bool:
    return True


def market_session(now: datetime) -> str:
    return "market_open" if stock_market_is_open(now) else "off_hours"


def next_auto_refresh_at(now: datetime, interval_seconds: int) -> datetime:
    return as_aware_utc(now) + timedelta(seconds=interval_seconds)


def is_same_day(value: datetime | None, now: datetime) -> bool:
    return bool(value and to_taipei(value).date() == to_taipei(now).date())


def is_same_refresh_day(value: datetime | None, now: datetime) -> bool:
    return is_same_day(value, now)
