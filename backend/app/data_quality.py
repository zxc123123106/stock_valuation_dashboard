from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
import re
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db.models import (
    Stock,
    StockBrokerTrading,
    StockDailyPrice,
    StockDataQualityState,
    StockEPS,
    StockFinancialQuarter,
    StockMetric,
    StockMonthlyRevenue,
    StockPEHistory,
)
from .db.session import SessionLocal
from .db.apply import log_crawler_result


TAIPEI_TZ = ZoneInfo("Asia/Taipei")
QUALITY_CATEGORIES = (
    "QUOTE",
    "CURRENT_PE",
    "PE_HISTORY",
    "EPS",
    "FINANCIAL_QUARTER",
    "MONTHLY_REVENUE",
    "BROKER_TRADING",
    "TECHNICAL_DAILY",
)
RETRY_BACKOFF_SECONDS = (60, 180, 300, 900)
MARKET_OPEN_TIME = time(9, 0)
MARKET_CLOSE_TIME = time(13, 30)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def as_taipei(value: datetime | None) -> datetime | None:
    aware = as_utc(value)
    return aware.astimezone(TAIPEI_TZ) if aware else None


def error_summary(error: Exception | str) -> str:
    message = str(error)
    lower = message.lower()
    if "429" in lower or "rate limit" in lower or "too many requests" in lower:
        return "資料來源已達請求上限"
    if any(token in lower for token in ("401", "403", "unauthorized", "forbidden", "token", "api key")):
        return "資料來源認證失敗"
    if any(token in lower for token in ("timeout", "timed out", "connection", "dns", "name resolution")):
        return "資料來源連線逾時"
    if any(token in lower for token in ("no data", "not found", "暫無資料", "沒有資料", "empty")):
        return "資料來源暫無資料"
    if any(token in lower for token in ("parse", "decode", "json", "html", "selector")):
        return "資料來源格式或解析失敗"
    return "資料更新失敗"


def get_or_create_state(session: Session, stock_id: int, category: str) -> StockDataQualityState:
    state = session.scalar(
        select(StockDataQualityState).where(
            StockDataQualityState.stock_id == stock_id,
            StockDataQualityState.category == category,
        )
    )
    if state is None:
        state = StockDataQualityState(stock_id=stock_id, category=category)
        session.add(state)
        session.flush()
    return state


def record_quality_success(
    session: Session,
    stock: Stock,
    category: str,
    *,
    attempted_at: datetime,
    data_date: date | None = None,
    data_period: str | None = None,
    fetched_at: datetime | None = None,
    source: str | None = None,
) -> None:
    state = get_or_create_state(session, stock.id, category)
    now = datetime.now(UTC)
    state.data_date = data_date
    state.data_period = data_period
    state.fetched_at = fetched_at or now
    state.source = source
    state.sync_status = "success"
    state.last_attempt_at = attempted_at
    state.last_success_at = now
    state.last_error_summary = None
    state.last_error_detail = None
    state.last_error_at = None
    state.failure_count = 0
    state.next_retry_at = None
    state.is_cached = False
    state.updated_at = now


def record_quality_failure(
    symbol: str,
    category: str,
    error: Exception | str,
    *,
    attempted_at: datetime,
) -> None:
    with SessionLocal() as session:
        stock = session.scalar(select(Stock).where(Stock.symbol == symbol, Stock.is_active.is_(True)))
        if stock is None:
            return
        state = get_or_create_state(session, stock.id, category)
        now = datetime.now(UTC)
        failure_count = (state.failure_count or 0) + 1
        state.last_attempt_at = attempted_at
        state.last_error_summary = error_summary(error)
        state.last_error_detail = str(error)[:4000]
        state.last_error_at = now
        state.failure_count = failure_count
        state.next_retry_at = now + timedelta(
            seconds=RETRY_BACKOFF_SECONDS[min(failure_count - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
        )
        state.is_cached = state.last_success_at is not None or state.fetched_at is not None
        state.sync_status = "retry_wait"
        state.updated_at = now
        log_crawler_result(
            session,
            f"data_refresh:{symbol}:{category}",
            "FAILED",
            str(error),
            attempted_at,
            now,
        )
        session.commit()


def mark_quality_sync_status(symbol: str, category: str, sync_status: str, at: datetime) -> None:
    with SessionLocal() as session:
        stock = session.scalar(select(Stock).where(Stock.symbol == symbol, Stock.is_active.is_(True)))
        if stock is None:
            return
        state = get_or_create_state(session, stock.id, category)
        state.sync_status = sync_status
        state.last_attempt_at = at
        state.updated_at = at
        session.commit()


def quality_retry_due(symbol: str, category: str, now: datetime, *, force: bool = False) -> bool:
    if force:
        return True
    with SessionLocal() as session:
        stock_id = session.scalar(select(Stock.id).where(Stock.symbol == symbol, Stock.is_active.is_(True)))
        if stock_id is None:
            return False
        state = session.scalar(
            select(StockDataQualityState).where(
                StockDataQualityState.stock_id == stock_id,
                StockDataQualityState.category == category,
            )
        )
        return state is None or state.next_retry_at is None or as_utc(state.next_retry_at) <= as_utc(now)


def latest_official_trade_date(session: Session) -> date | None:
    daily_date = session.scalar(select(func.max(StockDailyPrice.trade_date)))
    pe_date = session.scalar(select(func.max(StockPEHistory.trade_date)))
    candidates = [value for value in (daily_date, pe_date) if value is not None]
    return max(candidates) if candidates else None


def _known_trade_dates(session: Session) -> list[date]:
    values = session.scalars(select(StockDailyPrice.trade_date).distinct().order_by(StockDailyPrice.trade_date)).all()
    return list(values)


def _trade_date_lag(session: Session, data_date: date | None, expected: date | None) -> int | None:
    if data_date is None or expected is None:
        return None
    if data_date >= expected:
        return 0
    known = [value for value in _known_trade_dates(session) if data_date < value <= expected]
    if known:
        return len(known)
    lag = 0
    cursor = data_date
    while cursor < expected:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            lag += 1
    return lag


def _quarter_index(period: str | None) -> int | None:
    match = re.search(r"(\d{4})Q([1-4])", period or "")
    return int(match.group(1)) * 4 + int(match.group(2)) - 1 if match else None


def _month_index(period: str | None) -> int | None:
    match = re.search(r"(\d{4})[-/](\d{1,2})", period or "")
    return int(match.group(1)) * 12 + int(match.group(2)) - 1 if match else None


def expected_quarter_period(now: datetime) -> str:
    local = as_taipei(now)
    current = local.date()
    year = current.year
    deadlines = (
        (date(year, 4, 3), f"{year - 1}Q4"),
        (date(year, 5, 18), f"{year}Q1"),
        (date(year, 8, 17), f"{year}Q2"),
        (date(year, 11, 17), f"{year}Q3"),
    )
    available = [period for deadline, period in deadlines if current >= deadline]
    return available[-1] if available else f"{year - 1}Q3"


def expected_month_period(now: datetime) -> str:
    local = as_taipei(now)
    current = local.date()
    months_back = 1 if current.day >= 13 else 2
    year = current.year
    month = current.month - months_back
    while month <= 0:
        year -= 1
        month += 12
    return f"{year:04d}-{month:02d}"


def freshness_for_state(
    session: Session,
    state: StockDataQualityState | None,
    category: str,
    *,
    now: datetime,
    applicable: bool = True,
) -> str:
    if not applicable:
        return "NOT_APPLICABLE"
    if state is None or (state.data_date is None and not state.data_period and state.fetched_at is None):
        return "MISSING"
    local_now = as_taipei(now)
    if category == "QUOTE":
        fetched_at = as_utc(state.fetched_at or state.last_success_at)
        if fetched_at is None:
            return "MISSING"
        is_market_open = local_now.weekday() < 5 and MARKET_OPEN_TIME <= local_now.time() < MARKET_CLOSE_TIME
        if is_market_open and state.data_date == local_now.date():
            age = as_utc(now) - fetched_at
            if age <= timedelta(minutes=2):
                return "REALTIME"
            if age <= timedelta(minutes=10):
                return "CURRENT"
            if age <= timedelta(minutes=30):
                return "DELAYED"
            return "STALE"
        lag = _trade_date_lag(session, state.data_date, latest_official_trade_date(session))
        return "CURRENT" if lag in (None, 0) else "DELAYED" if lag == 1 else "STALE"
    if category in {"CURRENT_PE", "PE_HISTORY", "BROKER_TRADING", "TECHNICAL_DAILY"}:
        expected = latest_official_trade_date(session)
        if category == "CURRENT_PE" and local_now.time() < time(18, 0) and expected == local_now.date():
            prior = [value for value in _known_trade_dates(session) if value < expected]
            expected = prior[-1] if prior else expected
        lag = _trade_date_lag(session, state.data_date, expected)
        if lag is None:
            return "MISSING"
        return "CURRENT" if lag == 0 else "DELAYED" if lag == 1 else "STALE"
    if category in {"EPS", "FINANCIAL_QUARTER"}:
        actual = _quarter_index(state.data_period)
        expected = _quarter_index(expected_quarter_period(now))
        if actual is None:
            return "MISSING"
        lag = expected - actual
        return "CURRENT" if lag <= 0 else "DELAYED" if lag == 1 else "STALE"
    if category == "MONTHLY_REVENUE":
        actual = _month_index(state.data_period)
        expected = _month_index(expected_month_period(now))
        if actual is None:
            return "MISSING"
        lag = expected - actual
        return "CURRENT" if lag <= 0 else "DELAYED" if lag == 1 else "STALE"
    return "CURRENT"


def backfill_data_quality_states() -> None:
    with SessionLocal() as session:
        stocks = session.scalars(select(Stock)).all()
        for stock in stocks:
            _backfill_stock(session, stock)
        session.commit()


def _backfill_stock(session: Session, stock: Stock) -> None:
    metric = session.scalar(
        select(StockMetric).where(StockMetric.stock_id == stock.id).order_by(StockMetric.created_at.desc()).limit(1)
    )
    if metric:
        _backfill_state(
            session, stock, "QUOTE", data_date=as_taipei(metric.price_updated_at).date(),
            fetched_at=metric.created_at, source=metric.source,
        )
        if metric.current_pe and metric.current_pe > 0:
            _backfill_state(
                session, stock, "CURRENT_PE", data_date=metric.pe_data_date,
                fetched_at=metric.pe_updated_at, source=metric.source,
            )
    pe = session.scalar(select(StockPEHistory).where(StockPEHistory.stock_id == stock.id).order_by(StockPEHistory.trade_date.desc()).limit(1))
    if pe:
        _backfill_state(session, stock, "PE_HISTORY", data_date=pe.trade_date, fetched_at=pe.fetched_at, source=pe.source)
    eps = session.scalar(select(StockEPS).where(StockEPS.stock_id == stock.id).order_by(StockEPS.eps_updated_at.desc()).limit(1))
    quarter = session.scalar(select(StockFinancialQuarter).where(StockFinancialQuarter.stock_id == stock.id).order_by(StockFinancialQuarter.quarter_date.desc()).limit(1))
    revenue = session.scalar(select(StockMonthlyRevenue).where(StockMonthlyRevenue.stock_id == stock.id).order_by(StockMonthlyRevenue.month_date.desc()).limit(1))
    broker = session.scalar(select(StockBrokerTrading).where(StockBrokerTrading.stock_id == stock.id))
    daily = session.scalar(select(StockDailyPrice).where(StockDailyPrice.stock_id == stock.id).order_by(StockDailyPrice.trade_date.desc()).limit(1))
    if eps:
        period = (eps.eps_period or "").split("+")[0].strip()
        _backfill_state(session, stock, "EPS", data_period=period, fetched_at=eps.eps_updated_at, source=eps.source)
    if quarter:
        _backfill_state(session, stock, "FINANCIAL_QUARTER", data_period=_quarter_label(quarter.quarter_date), fetched_at=quarter.fetched_at, source=quarter.source)
    if revenue:
        _backfill_state(session, stock, "MONTHLY_REVENUE", data_period=revenue.month_date.strftime("%Y-%m"), fetched_at=revenue.fetched_at, source=revenue.source)
    if broker:
        try:
            broker_date = date.fromisoformat(broker.trade_date.replace("/", "-"))
        except ValueError:
            broker_date = None
        _backfill_state(session, stock, "BROKER_TRADING", data_date=broker_date, fetched_at=broker.fetched_at, source=broker.source)
    if daily:
        _backfill_state(session, stock, "TECHNICAL_DAILY", data_date=daily.trade_date, fetched_at=daily.fetched_at, source=daily.source)


def _quarter_label(value: date) -> str:
    return f"{value.year}Q{((value.month - 1) // 3) + 1}"


def _backfill_state(
    session: Session,
    stock: Stock,
    category: str,
    *,
    data_date: date | None = None,
    data_period: str | None = None,
    fetched_at: datetime | None = None,
    source: str | None = None,
) -> None:
    existing = session.scalar(
        select(StockDataQualityState).where(
            StockDataQualityState.stock_id == stock.id,
            StockDataQualityState.category == category,
        )
    )
    if existing is not None:
        return
    state = get_or_create_state(session, stock.id, category)
    state.data_date = data_date
    state.data_period = data_period
    state.fetched_at = fetched_at
    state.source = source
    state.last_attempt_at = fetched_at
    state.last_success_at = fetched_at
    state.is_cached = False
    state.sync_status = "success"
