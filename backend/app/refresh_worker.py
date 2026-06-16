from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .database import (
    CrawlerLog,
    SessionLocal,
    Stock,
    StockBrokerTrading,
    StockDailyPrice,
    StockEPS,
    StockFinancialQuarter,
    StockMetric,
    StockMonthlyRevenue,
    StockPEHistory,
    StockRefreshState,
    apply_broker_trading_snapshot,
    apply_daily_price_snapshots,
    apply_financial_quarter_snapshots,
    apply_layered_stock_refresh,
    apply_monthly_revenue_snapshots,
    apply_pe_history_snapshots,
    cleanup_crawler_logs_if_due,
    log_crawler_result,
    next_display_order,
)
from .finmind_daily import fetch_daily_prices
from .market_data import (
    derive_pe,
    fetch_financial_quarters,
    fetch_monthly_revenues,
    fetch_pe_history,
    fetch_stock_eps,
    fetch_stock_pe,
    fetch_stock_profile,
    fetch_stock_quote,
    normalize_symbol,
    StockProfileSnapshot,
)
from .yahoo_broker import fetch_broker_trading


TAIPEI_TZ = ZoneInfo("Asia/Taipei")
MARKET_OPEN_TIME = time(9, 0)
MARKET_AUTO_REFRESH_END_TIME = time(14, 0)
REFRESH_WINDOW_LABEL = "平日 09:00-14:00 Asia/Taipei"
CLOSE_VERIFICATION_JOB_NAME = "market_close_verification"
RETRY_BACKOFF_SECONDS = (60, 180, 300, 900)


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
    force_full: bool = False


class BackgroundRefreshManager:
    def __init__(self, interval_seconds: int, finmind_token: str | None = None) -> None:
        self.interval_seconds = interval_seconds
        self.finmind_token = finmind_token
        self._queue: asyncio.Queue[RefreshJob] | None = None
        self._queued_symbols: set[str] = set()
        self._queued_force_full: dict[str, bool] = {}
        self._queued_at: dict[str, datetime] = {}
        self._states: dict[str, RefreshSymbolState] = {}
        self._lock = asyncio.Lock()
        self._stop_event: asyncio.Event | None = None
        self._consumer_task: asyncio.Task | None = None
        self._ticker_task: asyncio.Task | None = None
        self._current_symbol: str | None = None
        self._deleted_symbols: set[str] = set()
        self._next_auto_refresh_at: datetime | None = None
        self._last_refresh_finished_at: datetime | None = None

    async def start(self) -> None:
        if self._consumer_task and not self._consumer_task.done():
            return

        self._queue = asyncio.Queue()
        self._stop_event = asyncio.Event()
        self._consumer_task = asyncio.create_task(self._consume_queue(), name="stock-refresh-consumer")
        self._ticker_task = asyncio.create_task(self._run_ticker(), name="stock-refresh-ticker")

    async def stop(self) -> None:
        if self._stop_event:
            self._stop_event.set()

        tasks = [task for task in (self._consumer_task, self._ticker_task) if task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def queue_symbol(
        self,
        symbol: str,
        *,
        create_placeholder: bool = False,
        force_full: bool = False,
    ) -> RefreshSymbolState:
        normalized_symbol = normalize_symbol(symbol)
        if create_placeholder:
            await asyncio.to_thread(_ensure_active_placeholder, normalized_symbol)

        now = datetime.now(UTC)
        bypass_backoff = create_placeholder or force_full
        if not bypass_backoff:
            retry_wait_state = await asyncio.to_thread(_retry_wait_state, normalized_symbol, now)
            if retry_wait_state:
                return retry_wait_state

        enqueue_job = False
        response_state: RefreshSymbolState | None = None
        queued_record_message: str | None = None
        async with self._lock:
            self._deleted_symbols.discard(normalized_symbol)
            existing = self._states.get(normalized_symbol)
            if normalized_symbol in self._queued_symbols:
                if force_full:
                    self._queued_force_full[normalized_symbol] = True
                    if existing and existing.status == "queued":
                        existing.message = "Queued for full data refresh."
                    queued_record_message = "Queued for full data refresh."
                response_state = existing or RefreshSymbolState(
                    symbol=normalized_symbol,
                    status="queued",
                    queued_at=self._queued_at.get(normalized_symbol, now),
                    message="Already queued for full data refresh." if force_full else "Already queued.",
                )
            elif normalized_symbol == self._current_symbol:
                if force_full:
                    self._queued_symbols.add(normalized_symbol)
                    self._queued_force_full[normalized_symbol] = True
                    self._queued_at[normalized_symbol] = now
                    enqueue_job = True
                    queued_record_message = "Queued for full data refresh after current run."
                    response_state = RefreshSymbolState(
                        symbol=normalized_symbol,
                        status="queued",
                        queued_at=now,
                        started_at=existing.started_at if existing else None,
                        message="Queued for full data refresh after current run.",
                    )
                else:
                    response_state = existing or RefreshSymbolState(
                        symbol=normalized_symbol,
                        status="refreshing",
                        queued_at=now,
                        started_at=now,
                        message="Already refreshing.",
                    )
            else:
                state = RefreshSymbolState(
                    symbol=normalized_symbol,
                    status="queued",
                    queued_at=now,
                    message="Queued for full data refresh." if force_full else "Queued for background refresh.",
                )
                self._states[normalized_symbol] = state
                self._queued_symbols.add(normalized_symbol)
                self._queued_force_full[normalized_symbol] = force_full
                self._queued_at[normalized_symbol] = now
                enqueue_job = True
                queued_record_message = state.message
                response_state = state

        if queued_record_message:
            await asyncio.to_thread(_record_refresh_queued, normalized_symbol, now, queued_record_message)

        if enqueue_job:
            await self._put_queue(RefreshJob(normalized_symbol, force_full=force_full))
        return response_state

    async def forget_symbol(self, symbol: str) -> None:
        normalized_symbol = normalize_symbol(symbol)
        async with self._lock:
            self._deleted_symbols.add(normalized_symbol)
            self._queued_symbols.discard(normalized_symbol)
            self._queued_force_full.pop(normalized_symbol, None)
            self._queued_at.pop(normalized_symbol, None)
            self._states.pop(normalized_symbol, None)
            if self._current_symbol == normalized_symbol:
                self._current_symbol = None

    async def queue_active_stocks(self, *, force_full: bool = False) -> list[RefreshSymbolState]:
        symbols = await asyncio.to_thread(_active_symbols)
        states = []
        for symbol in symbols:
            states.append(await self.queue_symbol(symbol, force_full=force_full))
        return states

    async def snapshot(self) -> dict:
        now = datetime.now(UTC)
        auto_refresh_enabled = _auto_refresh_enabled(now)
        next_auto_refresh_at = _next_auto_refresh_at(now, self.interval_seconds)
        last_close_verification_at = await asyncio.to_thread(_last_close_verification_at)
        symbol_states = await asyncio.to_thread(_refresh_state_rows)
        async with self._lock:
            status = "idle"
            if self._current_symbol:
                status = "refreshing"
            elif self._queued_symbols:
                status = "queued"

            return {
                "status": status,
                "current_symbol": self._current_symbol,
                "queue_length": len(self._queued_symbols),
                "auto_refresh_enabled": auto_refresh_enabled,
                "market_session": _market_session(now),
                "refresh_window": REFRESH_WINDOW_LABEL,
                "next_auto_refresh_at": self._next_auto_refresh_at if auto_refresh_enabled and self._next_auto_refresh_at else next_auto_refresh_at,
                "last_refresh_finished_at": self._last_refresh_finished_at,
                "last_close_verification_at": last_close_verification_at,
                "symbols": symbol_states,
            }

    async def _put_queue(self, job: RefreshJob) -> None:
        if not self._queue:
            return
        await self._queue.put(job)

    async def _consume_queue(self) -> None:
        if not self._queue:
            return

        while True:
            job = await self._queue.get()
            try:
                await self._refresh_symbol(job)
            finally:
                self._queue.task_done()

    async def _run_ticker(self) -> None:
        if not self._stop_event:
            return

        while True:
            now = datetime.now(UTC)
            async with self._lock:
                self._next_auto_refresh_at = _next_auto_refresh_at(now, self.interval_seconds)

            if _auto_refresh_enabled(now):
                await self.queue_active_stocks()
            elif await asyncio.to_thread(_close_verification_due, now):
                await self._run_close_verification()

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_seconds)
                return
            except TimeoutError:
                continue

    async def _run_close_verification(self) -> None:
        started_at = datetime.now(UTC)
        states = await self.queue_active_stocks()
        if self._queue:
            await self._queue.join()
        await asyncio.to_thread(_log_close_verification_result, started_at, len(states))
        await asyncio.to_thread(cleanup_crawler_logs_if_due)

    async def _refresh_symbol(self, job: RefreshJob) -> None:
        started_at = datetime.now(UTC)
        symbol = job.symbol
        async with self._lock:
            self._queued_symbols.discard(symbol)
            force_full = self._queued_force_full.pop(symbol, job.force_full)
            queued_at = self._queued_at.pop(symbol, None)
            if symbol in self._deleted_symbols:
                self._states.pop(symbol, None)
                return

            self._current_symbol = symbol
            self._states[symbol] = RefreshSymbolState(
                symbol=symbol,
                status="refreshing",
                message="Refreshing all cached data." if force_full else "Refreshing market data.",
                queued_at=queued_at or self._states.get(symbol, RefreshSymbolState(symbol, "queued")).queued_at,
                started_at=started_at,
            )

        await asyncio.to_thread(
            _record_refresh_running,
            symbol,
            started_at,
            queued_at,
            "Refreshing all cached data." if force_full else "Refreshing market data.",
        )

        try:
            message = await asyncio.to_thread(
                _refresh_symbol_sync,
                symbol,
                started_at,
                force_full=force_full,
                finmind_token=self.finmind_token,
            )
        except Exception as exc:
            finished_at = datetime.now(UTC)
            await asyncio.to_thread(_record_refresh_failed, symbol, str(exc), started_at, finished_at)
            async with self._lock:
                if symbol in self._deleted_symbols:
                    self._states.pop(symbol, None)
                    if self._current_symbol == symbol:
                        self._current_symbol = None
                    self._last_refresh_finished_at = finished_at
                    return

                self._states[symbol] = RefreshSymbolState(
                    symbol=symbol,
                    status="failed",
                    message=str(exc),
                    queued_at=self._states.get(symbol, RefreshSymbolState(symbol, "queued")).queued_at,
                    started_at=started_at,
                    finished_at=finished_at,
                )
                self._current_symbol = None
                self._last_refresh_finished_at = finished_at
            return

        finished_at = datetime.now(UTC)
        await asyncio.to_thread(_record_refresh_success, symbol, message, started_at, finished_at)
        async with self._lock:
            if symbol in self._deleted_symbols:
                self._states.pop(symbol, None)
                if self._current_symbol == symbol:
                    self._current_symbol = None
                self._last_refresh_finished_at = finished_at
                return

            self._states[symbol] = RefreshSymbolState(
                symbol=symbol,
                status="success",
                message=message,
                queued_at=self._states.get(symbol, RefreshSymbolState(symbol, "queued")).queued_at,
                started_at=started_at,
                finished_at=finished_at,
            )
            self._current_symbol = None
            self._last_refresh_finished_at = finished_at


def _active_symbols() -> list[str]:
    with SessionLocal() as session:
        return session.scalars(
            select(Stock.symbol)
            .where(Stock.is_active.is_(True))
            .order_by(Stock.display_order, Stock.symbol)
        ).all()


def _active_stock_exists(symbol: str) -> bool:
    with SessionLocal() as session:
        return bool(
            session.scalar(
                select(Stock.id).where(Stock.symbol == symbol, Stock.is_active.is_(True))
            )
        )


def _refresh_state_rows() -> list[dict]:
    with SessionLocal() as session:
        active_stocks = session.scalars(
            select(Stock).where(Stock.is_active.is_(True)).order_by(Stock.display_order, Stock.symbol)
        ).all()
        states = {
            state.symbol: state
            for state in session.scalars(select(StockRefreshState)).all()
        }
        return [
            _refresh_state_response(
                states.get(stock.symbol)
                or StockRefreshState(symbol=stock.symbol, status="idle", message="")
            )
            for stock in active_stocks
        ]


def _refresh_state_response(state: StockRefreshState) -> dict:
    return {
        "symbol": state.symbol,
        "status": state.status,
        "message": state.message,
        "failure_count": state.failure_count or 0,
        "last_error": state.last_error,
        "next_retry_at": state.next_retry_at,
        "queued_at": state.queued_at,
        "started_at": state.started_at,
        "finished_at": state.finished_at,
    }


def _retry_wait_state(symbol: str, now: datetime) -> RefreshSymbolState | None:
    with SessionLocal() as session:
        state = session.scalar(select(StockRefreshState).where(StockRefreshState.symbol == symbol))
        if not state or not state.next_retry_at or state.status not in {"failed", "retry_wait"}:
            return None

        next_retry_at = _as_aware_utc(state.next_retry_at)
        if next_retry_at <= _as_aware_utc(now):
            return None

        state.status = "retry_wait"
        state.message = "更新失敗，等待自動重試；目前使用快取"
        state.updated_at = now
        session.commit()
        return RefreshSymbolState(
            symbol=symbol,
            status="retry_wait",
            message=state.message,
            queued_at=state.queued_at,
            started_at=state.started_at,
            finished_at=state.finished_at,
        )


def _record_refresh_queued(symbol: str, queued_at: datetime, message: str) -> None:
    with SessionLocal() as session:
        if not _active_stock_exists_in_session(session, symbol):
            return
        state = _get_or_create_refresh_state(session, symbol)
        state.status = "queued"
        state.message = message
        state.queued_at = queued_at
        state.next_retry_at = None
        state.updated_at = queued_at
        session.commit()


def _record_refresh_running(symbol: str, started_at: datetime, queued_at: datetime | None, message: str) -> None:
    with SessionLocal() as session:
        if not _active_stock_exists_in_session(session, symbol):
            return
        state = _get_or_create_refresh_state(session, symbol)
        state.status = "running"
        state.message = message
        state.queued_at = queued_at or state.queued_at
        state.started_at = started_at
        state.updated_at = started_at
        session.commit()


def _record_refresh_success(symbol: str, message: str, started_at: datetime, finished_at: datetime) -> None:
    with SessionLocal() as session:
        if not _active_stock_exists_in_session(session, symbol):
            return
        state = _get_or_create_refresh_state(session, symbol)
        state.status = "success"
        state.message = message
        state.failure_count = 0
        state.last_error = None
        state.next_retry_at = None
        state.started_at = started_at
        state.finished_at = finished_at
        state.updated_at = finished_at
        session.commit()


def _record_refresh_failed(symbol: str, error: str, started_at: datetime, finished_at: datetime) -> None:
    with SessionLocal() as session:
        if not _active_stock_exists_in_session(session, symbol):
            return
        state = _get_or_create_refresh_state(session, symbol)
        failure_count = (state.failure_count or 0) + 1
        state.status = "failed"
        state.message = "更新失敗，使用快取"
        state.failure_count = failure_count
        state.last_error = error[:500]
        state.next_retry_at = finished_at + timedelta(seconds=_retry_delay_seconds(failure_count))
        state.started_at = started_at
        state.finished_at = finished_at
        state.updated_at = finished_at
        log_crawler_result(
            session=session,
            job_name=f"market_refresh:{symbol}",
            status="FAILED",
            message=error,
            started_at=started_at,
            finished_at=finished_at,
        )
        session.commit()


def _get_or_create_refresh_state(session, symbol: str) -> StockRefreshState:
    state = session.scalar(select(StockRefreshState).where(StockRefreshState.symbol == symbol))
    if not state:
        state = StockRefreshState(symbol=symbol, status="idle", message="")
        session.add(state)
        session.flush()
    return state


def _active_stock_exists_in_session(session, symbol: str) -> bool:
    stock_id = session.scalar(
        select(Stock.id).where(Stock.symbol == symbol, Stock.is_active.is_(True))
    )
    return stock_id is not None


def _retry_delay_seconds(failure_count: int) -> int:
    index = max(0, min(failure_count - 1, len(RETRY_BACKOFF_SECONDS) - 1))
    return RETRY_BACKOFF_SECONDS[index]


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _to_taipei(value: datetime) -> datetime:
    return _as_aware_utc(value).astimezone(TAIPEI_TZ)


def _is_weekday(value: datetime) -> bool:
    return _to_taipei(value).weekday() < 5


def _auto_refresh_enabled(now: datetime) -> bool:
    local_now = _to_taipei(now)
    return (
        local_now.weekday() < 5
        and MARKET_OPEN_TIME <= local_now.time() < MARKET_AUTO_REFRESH_END_TIME
    )


def _market_session(now: datetime) -> str:
    local_now = _to_taipei(now)
    if local_now.weekday() >= 5:
        return "weekend"
    if local_now.time() < MARKET_OPEN_TIME:
        return "pre_open"
    if local_now.time() < MARKET_AUTO_REFRESH_END_TIME:
        return "open"
    return "post_close"


def _next_market_open(now: datetime) -> datetime:
    local_now = _to_taipei(now)
    candidate = local_now.replace(
        hour=MARKET_OPEN_TIME.hour,
        minute=MARKET_OPEN_TIME.minute,
        second=0,
        microsecond=0,
    )
    if local_now.weekday() < 5 and local_now < candidate:
        return candidate.astimezone(UTC)

    candidate = candidate + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate = candidate + timedelta(days=1)
    return candidate.astimezone(UTC)


def _next_auto_refresh_at(now: datetime, interval_seconds: int) -> datetime:
    if _auto_refresh_enabled(now):
        return _as_aware_utc(now) + timedelta(seconds=interval_seconds)
    return _next_market_open(now)


def _is_same_day(value: datetime | None, now: datetime) -> bool:
    if not value:
        return False
    return _to_taipei(value).date() == _to_taipei(now).date()


def _is_same_refresh_day(value: datetime | None, now: datetime) -> bool:
    if not value:
        return False

    local_value = _to_taipei(value)
    local_now = _to_taipei(now)
    if local_value.date() != local_now.date():
        return False

    if local_now.time() >= MARKET_OPEN_TIME:
        return local_value.time() >= MARKET_OPEN_TIME
    return True


def _last_close_verification_at() -> datetime | None:
    with SessionLocal() as session:
        finished_at = session.scalar(
            select(CrawlerLog.finished_at)
            .where(
                CrawlerLog.job_name == CLOSE_VERIFICATION_JOB_NAME,
                CrawlerLog.status == "SUCCESS",
            )
            .order_by(CrawlerLog.finished_at.desc())
            .limit(1)
        )
    return _as_aware_utc(finished_at) if finished_at else None


def _close_verification_due(now: datetime) -> bool:
    if not _is_weekday(now) or _to_taipei(now).time() < MARKET_AUTO_REFRESH_END_TIME:
        return False

    last_finished_at = _last_close_verification_at()
    return not _is_same_day(last_finished_at, now)


def _log_close_verification_result(started_at: datetime, symbols_count: int) -> None:
    with SessionLocal() as session:
        log_crawler_result(
            session=session,
            job_name=CLOSE_VERIFICATION_JOB_NAME,
            status="SUCCESS",
            message=f"Close verification refreshed {symbols_count} active symbols.",
            started_at=started_at,
        )
        session.commit()


def _refresh_due(symbol: str, now: datetime) -> tuple[bool, bool]:
    with SessionLocal() as session:
        stock = session.scalar(select(Stock).where(Stock.symbol == symbol))
        if not stock:
            return True, True

        latest_metric = session.scalar(
            select(StockMetric)
            .where(StockMetric.stock_id == stock.id)
            .order_by(StockMetric.created_at.desc())
            .limit(1)
        )
        latest_eps_updated_at = session.scalar(
            select(StockEPS.eps_updated_at)
            .where(StockEPS.stock_id == stock.id)
            .order_by(StockEPS.eps_updated_at.desc())
            .limit(1)
        )
        return (
            latest_metric is None or not _is_same_refresh_day(latest_metric.pe_updated_at, now),
            latest_eps_updated_at is None or not _is_same_refresh_day(latest_eps_updated_at, now),
        )


def _broker_trading_due(symbol: str, now: datetime) -> bool:
    with SessionLocal() as session:
        stock = session.scalar(select(Stock).where(Stock.symbol == symbol))
        if not stock:
            return False

        latest_broker_trading = session.scalar(
            select(StockBrokerTrading)
            .where(StockBrokerTrading.stock_id == stock.id)
            .limit(1)
        )
        return latest_broker_trading is None or not _is_same_day(latest_broker_trading.fetched_at, now)


def _daily_prices_due(symbol: str, now: datetime) -> bool:
    with SessionLocal() as session:
        stock = session.scalar(select(Stock).where(Stock.symbol == symbol))
        if not stock:
            return False

        latest_fetched_at = session.scalar(
            select(StockDailyPrice.fetched_at)
            .where(StockDailyPrice.stock_id == stock.id)
            .order_by(StockDailyPrice.fetched_at.desc())
            .limit(1)
        )
        return latest_fetched_at is None or not _is_same_day(latest_fetched_at, now)


def _analysis_table_due(symbol: str, now: datetime, model, fetched_at_column) -> bool:
    with SessionLocal() as session:
        stock = session.scalar(select(Stock).where(Stock.symbol == symbol))
        if not stock:
            return False

        latest_fetched_at = session.scalar(
            select(fetched_at_column)
            .select_from(model)
            .where(model.stock_id == stock.id)
            .order_by(fetched_at_column.desc())
            .limit(1)
        )
        return latest_fetched_at is None or not _is_same_day(latest_fetched_at, now)


def _ensure_active_placeholder(symbol: str) -> None:
    now = datetime.now(UTC)
    with SessionLocal() as session:
        stock = session.scalar(select(Stock).where(Stock.symbol == symbol))
        if not stock:
            stock = Stock(
                symbol=symbol,
                name="更新中",
                market="TWSE",
                currency="TWD",
                is_active=True,
                display_order=next_display_order(session),
            )
            session.add(stock)
        elif not stock.is_active:
            stock.is_active = True
            stock.display_order = next_display_order(session)
        stock.updated_at = now
        session.commit()


def _cached_profile_snapshot(symbol: str) -> StockProfileSnapshot | None:
    with SessionLocal() as session:
        stock = session.scalar(select(Stock).where(Stock.symbol == symbol, Stock.is_active.is_(True)))
        if not stock:
            return None
        return StockProfileSnapshot(
            symbol=stock.symbol,
            name=stock.name,
            asset_type=stock.asset_type,
            market=stock.market,
            currency=stock.currency,
        )


def _refresh_symbol_sync(
    symbol: str,
    started_at: datetime,
    *,
    force_full: bool = False,
    finmind_token: str | None = None,
) -> str:
    if not _active_stock_exists(symbol):
        return "標的已刪除，略過背景更新"

    pe_due, eps_due = (True, True) if force_full else _refresh_due(symbol, started_at)
    broker_trading_due = True if force_full else _broker_trading_due(symbol, started_at)
    daily_prices_due = True if force_full else _daily_prices_due(symbol, started_at)
    pe_history_due = True if force_full else _analysis_table_due(
        symbol,
        started_at,
        StockPEHistory,
        StockPEHistory.fetched_at,
    )
    monthly_revenue_due = True if force_full else _analysis_table_due(
        symbol,
        started_at,
        StockMonthlyRevenue,
        StockMonthlyRevenue.fetched_at,
    )
    financial_quarters_due = True if force_full else _analysis_table_due(
        symbol,
        started_at,
        StockFinancialQuarter,
        StockFinancialQuarter.fetched_at,
    )
    calculated_at = datetime.now(UTC)
    messages = ["全量刷新：股價已更新" if force_full else "股價已更新"]
    source_parts: list[str] = []

    try:
        profile = fetch_stock_profile(symbol, finmind_token=finmind_token)
    except Exception as exc:
        profile = _cached_profile_snapshot(symbol)
        if not profile:
            raise
        messages.append(f"profile 更新失敗，沿用快取：{exc}")
        source_parts.append("cached profile")

    quote = fetch_stock_quote(symbol, profile=profile, finmind_token=finmind_token)
    source_parts.append(quote.source)
    current_pe = None
    pe_updated_at = None
    eps_rows = None
    eps_updated_at = None
    broker_trading = None
    daily_prices = None
    pe_history = None
    monthly_revenues = None
    financial_quarters = None

    if profile.asset_type == "ETF":
        messages.append("ETF 僅更新現價")
    elif pe_due:
        try:
            current_pe = fetch_stock_pe(
                symbol,
                finmind_token=finmind_token,
                end_date=started_at.astimezone(TAIPEI_TZ).date(),
            )
        except Exception as exc:
            messages.append(f"PE 更新失敗，沿用快取：{exc}")
            source_parts.append("cached PE")
        else:
            if current_pe is None:
                messages.append("TWSE PE 暫無資料，沿用快取")
                source_parts.append("cached PE")
            else:
                pe_updated_at = calculated_at
                messages.append("PE 已按日更新")
                source_parts.append("TWSE/FinMind daily PE")
    else:
        messages.append("PE 沿用今日快取")
        source_parts.append("cached daily PE")

    if profile.asset_type == "ETF":
        pass
    elif eps_due:
        try:
            eps_rows = fetch_stock_eps(
                symbol,
                finmind_token=finmind_token,
                end_date=started_at.astimezone(TAIPEI_TZ).date(),
            )
        except Exception as exc:
            messages.append(f"EPS 更新失敗，沿用快取：{exc}")
            source_parts.append("cached EPS")
        else:
            eps_updated_at = calculated_at
            messages.append("EPS 已按日更新")
            source_parts.append("FinMind financial EPS")
    else:
        messages.append("EPS 沿用今日快取")
        source_parts.append("cached daily EPS")

    if current_pe is None and eps_rows:
        current_pe = derive_pe(quote.current_price, eps_rows)
        pe_updated_at = calculated_at
        messages.append("PE 已由最新 EPS 推算")
        source_parts.append("derived PE")

    if broker_trading_due:
        try:
            broker_trading = fetch_broker_trading(symbol)
        except Exception as exc:
            messages.append(f"主力進出更新失敗，沿用快取：{exc}")
        else:
            messages.append("主力進出已按日更新")
    else:
        messages.append("主力進出沿用今日快取")

    if daily_prices_due:
        try:
            daily_prices = fetch_daily_prices(
                symbol,
                token=finmind_token,
                end_date=started_at.astimezone(TAIPEI_TZ).date(),
            )
        except Exception as exc:
            messages.append(f"日線更新失敗，沿用快取：{exc}")
        else:
            messages.append("日線與 MA20 基礎資料已按日更新")
    else:
        messages.append("日線沿用今日快取")

    if profile.asset_type != "ETF" and pe_history_due:
        try:
            pe_history = fetch_pe_history(
                symbol,
                finmind_token=finmind_token,
                end_date=started_at.astimezone(TAIPEI_TZ).date(),
            )
        except Exception as exc:
            messages.append(f"三年 PE 更新失敗，沿用快取：{exc}")
        else:
            messages.append("三年 PE 已按日更新")
    elif profile.asset_type != "ETF":
        messages.append("三年 PE 沿用今日快取")

    if profile.asset_type != "ETF" and monthly_revenue_due:
        try:
            monthly_revenues = fetch_monthly_revenues(
                symbol,
                finmind_token=finmind_token,
                end_date=started_at.astimezone(TAIPEI_TZ).date(),
                months=36,
            )
        except Exception as exc:
            messages.append(f"月營收更新失敗，沿用快取：{exc}")
        else:
            messages.append("月營收已按日更新")
    elif profile.asset_type != "ETF":
        messages.append("月營收沿用今日快取")

    if profile.asset_type != "ETF" and financial_quarters_due:
        try:
            financial_quarters = fetch_financial_quarters(
                symbol,
                finmind_token=finmind_token,
                end_date=started_at.astimezone(TAIPEI_TZ).date(),
                quarters=12,
            )
        except Exception as exc:
            messages.append(f"季度基本面更新失敗，沿用快取：{exc}")
        else:
            messages.append("季度基本面已按日更新")
    elif profile.asset_type != "ETF":
        messages.append("季度基本面沿用今日快取")

    source = " + ".join(source_parts)
    with SessionLocal() as session:
        try:
            active_stock_id = session.scalar(
                select(Stock.id).where(Stock.symbol == symbol, Stock.is_active.is_(True))
            )
            if not active_stock_id:
                return "標的已刪除，略過背景更新"

            stock = apply_layered_stock_refresh(
                session,
                profile=profile,
                quote=quote,
                current_pe=current_pe,
                pe_updated_at=pe_updated_at,
                eps_rows=eps_rows,
                eps_updated_at=eps_updated_at,
                source=source,
                calculated_at=calculated_at,
            )
            if broker_trading:
                apply_broker_trading_snapshot(session, stock, broker_trading)
            if daily_prices:
                apply_daily_price_snapshots(session, stock, daily_prices)
            if pe_history:
                apply_pe_history_snapshots(session, stock, pe_history)
            if monthly_revenues:
                apply_monthly_revenue_snapshots(session, stock, monthly_revenues)
            if financial_quarters:
                apply_financial_quarter_snapshots(session, stock, financial_quarters)
            log_crawler_result(
                session=session,
                job_name=f"market_refresh:{symbol}",
                status="SUCCESS",
                message="; ".join(messages),
                started_at=started_at,
            )
            session.commit()
            session.refresh(stock)
        except Exception as exc:
            session.rollback()
            log_crawler_result(
                session=session,
                job_name=f"market_refresh:{symbol}",
                status="FAILED",
                message=str(exc),
                started_at=started_at,
            )
            session.commit()
            raise

    return "；".join(messages)
