from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..db.models import (
    CrawlerLog,
    Stock,
    StockBrokerTrading,
    StockDailyPrice,
    StockDataQualityState,
    StockEPS,
    StockFinancialQuarter,
    StockMetric,
    StockMonthlyRevenue,
    StockPEHistory,
    StockRefreshState,
)
from ..db.session import SessionLocal
from ..db.apply import (
    apply_fundamental_refresh,
    apply_broker_trading_snapshot,
    apply_daily_price_snapshots,
    apply_financial_quarter_snapshots,
    apply_layered_stock_refresh,
    apply_monthly_revenue_snapshots,
    apply_pe_history_snapshots,
    apply_quote_refresh,
    log_crawler_result,
    next_display_order,
)
from ..db.bootstrap import cleanup_crawler_logs_if_due
from ..services.database_backup_service import ensure_daily_backup
from ..services.market_data_service import (
    derive_pe,
    fetch_daily_prices,
    fetch_financial_bundle,
    fetch_financial_quarters,
    fetch_monthly_revenues,
    fetch_pe_history,
    fetch_stock_eps,
    fetch_stock_pe_snapshot,
    fetch_stock_profile,
    fetch_stock_quote,
    normalize_symbol,
    StockProfileSnapshot,
)
from ..taifex_futures import current_futures_session, refresh_wtx_futures_cache
from ..yahoo_broker import fetch_broker_trading
from ..data_quality import mark_quality_sync_status, quality_retry_due, record_quality_failure, record_quality_success
from .models import (
    BROKER_REFRESH_TIME,
    CHANNEL_BROKER,
    CHANNEL_CATEGORIES,
    CHANNEL_FUNDAMENTALS,
    CHANNEL_HISTORY,
    CHANNEL_QUOTE,
    CLOSE_VERIFICATION_JOB_NAME,
    DEFAULT_CHANNEL_TIMEOUT_SECONDS,
    FUNDAMENTAL_REFRESH_TIME,
    HISTORY_REFRESH_TIME,
    MARKET_CLOSE_TIME,
    MARKET_CLOSE_VERIFICATION_TIME,
    MARKET_OPEN_TIME,
    PRIORITY_AUTO,
    PRIORITY_MANUAL,
    PRIORITY_RETRY,
    REFRESH_CHANNELS,
    REFRESH_WINDOW_LABEL,
    RETRY_BACKOFF_SECONDS,
    SCHEDULER_TICK_SECONDS,
    STALE_PE_RETRY_INTERVAL,
    ChannelRuntime,
    RefreshJob,
    RefreshSymbolState,
)
from .scheduler import (
    as_aware_utc as _as_aware_utc,
    auto_refresh_enabled as _auto_refresh_enabled,
    expected_official_trade_date as _expected_official_trade_date,
    is_same_day as _is_same_day,
    is_same_refresh_day as _is_same_refresh_day,
    is_weekday as _is_weekday,
    market_session as _market_session,
    next_auto_refresh_at as _next_auto_refresh_at,
    previous_month_period as _previous_month_period,
    previous_weekday as _previous_weekday,
    stock_market_is_open as _stock_market_is_open,
    to_taipei as _to_taipei,
)


TAIPEI_TZ = ZoneInfo("Asia/Taipei")


class BackgroundRefreshManager:
    def __init__(
        self,
        interval_seconds: int,
        finmind_token: str | None = None,
        *,
        quote_market_interval_seconds: int | None = None,
        quote_off_hours_interval_seconds: int = 900,
        pe_poll_interval_seconds: int = 900,
        monthly_revenue_release_interval_seconds: int = 7200,
        futures_refresh_seconds: int = 10,
        channel_timeout_seconds: dict[str, float] | None = None,
    ) -> None:
        self.interval_seconds = interval_seconds
        self.quote_market_interval_seconds = quote_market_interval_seconds or interval_seconds
        self.quote_off_hours_interval_seconds = quote_off_hours_interval_seconds
        self.pe_poll_interval_seconds = pe_poll_interval_seconds
        self.monthly_revenue_release_interval_seconds = monthly_revenue_release_interval_seconds
        self.futures_refresh_seconds = futures_refresh_seconds
        self.channel_timeout_seconds = {
            **DEFAULT_CHANNEL_TIMEOUT_SECONDS,
            **(channel_timeout_seconds or {}),
        }
        self.finmind_token = finmind_token
        self._queues: dict[str, asyncio.PriorityQueue] = {}
        self._pending_jobs: dict[tuple[str, str], RefreshJob] = {}
        self._running_jobs: dict[tuple[str, str], RefreshJob] = {}
        self._follow_up_jobs: dict[tuple[str, str], RefreshJob] = {}
        self._states: dict[str, RefreshSymbolState] = {}
        self._channel_runtime = {channel: ChannelRuntime() for channel in REFRESH_CHANNELS}
        self._sequence = 0
        self._lock = asyncio.Lock()
        self._db_write_lock = asyncio.Lock()
        self._stop_event: asyncio.Event | None = None
        self._tasks: list[asyncio.Task] = []
        self._deleted_symbols: set[str] = set()
        self._next_quote_at: datetime | None = None
        self._next_schedule_scan_at: datetime | None = None
        self._last_refresh_finished_at: datetime | None = None
        self._finmind_semaphore: asyncio.Semaphore | None = None
        self._quote_semaphore: asyncio.Semaphore | None = None
        self._yahoo_semaphore: asyncio.Semaphore | None = None

    async def start(self) -> None:
        if self._tasks:
            return
        self._pending_jobs.clear()
        self._running_jobs.clear()
        self._follow_up_jobs.clear()
        self._deleted_symbols.clear()
        self._next_quote_at = None
        self._next_schedule_scan_at = None
        self._channel_runtime = {channel: ChannelRuntime() for channel in REFRESH_CHANNELS}
        self._queues = {channel: asyncio.PriorityQueue() for channel in REFRESH_CHANNELS}
        self._stop_event = asyncio.Event()
        self._finmind_semaphore = asyncio.Semaphore(1)
        self._quote_semaphore = asyncio.Semaphore(2)
        self._yahoo_semaphore = asyncio.Semaphore(1)
        for index in range(2):
            self._tasks.append(
                asyncio.create_task(self._consume_channel(CHANNEL_QUOTE), name=f"quote-refresh-{index + 1}")
            )
        for channel in (CHANNEL_FUNDAMENTALS, CHANNEL_BROKER, CHANNEL_HISTORY):
            self._tasks.append(
                asyncio.create_task(self._consume_channel(channel), name=f"{channel.lower()}-refresh")
            )
        self._tasks.append(asyncio.create_task(self._run_scheduler(), name="stock-refresh-scheduler"))
        self._tasks.append(asyncio.create_task(self._run_futures_ticker(), name="wtx-futures-refresh-ticker"))
        self._tasks.append(asyncio.create_task(self._run_database_backup_ticker(), name="database-backup-ticker"))

    async def stop(self) -> None:
        if self._stop_event:
            self._stop_event.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def _run_database_backup_ticker(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await asyncio.to_thread(ensure_daily_backup)
            except Exception:
                # Backup failures must not stop market-data refresh tasks.
                continue

    async def queue_symbol(
        self,
        symbol: str,
        *,
        create_placeholder: bool = False,
        force_full: bool = False,
    ) -> RefreshSymbolState:
        normalized_symbol = normalize_symbol(symbol)
        existed = await asyncio.to_thread(_active_stock_exists, normalized_symbol)
        if create_placeholder:
            await asyncio.to_thread(_ensure_active_placeholder, normalized_symbol)
        now = datetime.now(UTC)
        async with self._lock:
            self._deleted_symbols.discard(normalized_symbol)

        if force_full:
            await self._queue_all_channels(normalized_symbol, priority=PRIORITY_MANUAL, force=True)
            message = "Queued all refresh channels."
        else:
            await self._enqueue_job(
                RefreshJob(
                    symbol=normalized_symbol,
                    channel=CHANNEL_QUOTE,
                    categories=frozenset(("QUOTE",)),
                    priority=PRIORITY_MANUAL,
                    force_full=True,
                    profile_required=not existed,
                )
            )
            if existed:
                due = await asyncio.to_thread(
                    _scheduled_channel_categories,
                    normalized_symbol,
                    now,
                    self.pe_poll_interval_seconds,
                    self.monthly_revenue_release_interval_seconds,
                )
                for channel, categories in due.items():
                    if channel == CHANNEL_QUOTE or not categories:
                        continue
                    await self._enqueue_job(
                        RefreshJob(
                            symbol=normalized_symbol,
                            channel=channel,
                            categories=frozenset(categories),
                            priority=PRIORITY_MANUAL,
                        )
                    )
            message = "Queued quote refresh and due data channels."

        state = RefreshSymbolState(
            symbol=normalized_symbol,
            status="queued",
            message=message,
            queued_at=now,
        )
        async with self._lock:
            self._states[normalized_symbol] = state
        return state

    async def queue_active_stocks(self, *, force_full: bool = False) -> list[RefreshSymbolState]:
        symbols = await asyncio.to_thread(_active_symbols)
        now = datetime.now(UTC)
        states: list[RefreshSymbolState] = []
        for symbol in symbols:
            if force_full:
                await self._queue_all_channels(symbol, priority=PRIORITY_MANUAL, force=True)
            else:
                due = await asyncio.to_thread(
                    _scheduled_channel_categories,
                    symbol,
                    now,
                    self.pe_poll_interval_seconds,
                    self.monthly_revenue_release_interval_seconds,
                )
                for channel, categories in due.items():
                    if categories:
                        await self._enqueue_job(
                            RefreshJob(symbol, channel, frozenset(categories), PRIORITY_AUTO)
                        )
            state = RefreshSymbolState(
                symbol=symbol,
                status="queued",
                message="Queued all refresh channels." if force_full else "Queued due data channels.",
                queued_at=now,
            )
            states.append(state)
            async with self._lock:
                self._states[symbol] = state
        return states

    async def forget_symbol(self, symbol: str) -> None:
        normalized_symbol = normalize_symbol(symbol)
        async with self._lock:
            self._deleted_symbols.add(normalized_symbol)
            for mapping in (self._pending_jobs, self._running_jobs, self._follow_up_jobs):
                for key in [key for key in mapping if key[1] == normalized_symbol]:
                    mapping.pop(key, None)
            for runtime in self._channel_runtime.values():
                runtime.current_symbols.discard(normalized_symbol)
            self._states.pop(normalized_symbol, None)

    async def snapshot(self) -> dict:
        now = datetime.now(UTC)
        persisted_states = await asyncio.to_thread(_refresh_state_rows)
        last_close_verification_at = await asyncio.to_thread(_last_close_verification_at)
        async with self._lock:
            pending_count = len(self._pending_jobs)
            running_symbols = sorted({job.symbol for job in self._running_jobs.values()})
            current_symbol = running_symbols[0] if running_symbols else None
            status = "refreshing" if self._running_jobs else "queued" if pending_count else "idle"
            channels = {}
            next_times = []
            for channel in REFRESH_CHANNELS:
                runtime = self._channel_runtime[channel]
                queue_length = sum(1 for key in self._pending_jobs if key[0] == channel)
                channel_status = "refreshing" if runtime.current_symbols else "queued" if queue_length else "idle"
                if runtime.next_run_at:
                    next_times.append(runtime.next_run_at)
                channels[channel.lower()] = {
                    "status": channel_status,
                    "current_symbols": sorted(runtime.current_symbols),
                    "queue_length": queue_length,
                    "next_run_at": runtime.next_run_at,
                    "last_finished_at": runtime.last_finished_at,
                }

            runtime_by_symbol = {
                symbol: RefreshSymbolState(
                    symbol=symbol,
                    status="refreshing",
                    message="Refreshing data in one or more channels.",
                    started_at=self._states.get(symbol).started_at if self._states.get(symbol) else now,
                )
                for symbol in running_symbols
            }
            pending_symbols = {key[1] for key in self._pending_jobs}
            for symbol in pending_symbols - set(runtime_by_symbol):
                runtime_by_symbol[symbol] = RefreshSymbolState(
                    symbol=symbol,
                    status="queued",
                    message="Queued in one or more data channels.",
                    queued_at=self._states.get(symbol).queued_at if self._states.get(symbol) else now,
                )
            symbols = [
                _refresh_state_response(runtime_by_symbol.get(row["symbol"]) or row)
                for row in persisted_states
            ]
            next_auto_refresh_at = min(next_times) if next_times else _next_auto_refresh_at(now, self.interval_seconds)
            return {
                "status": status,
                "current_symbol": current_symbol,
                "queue_length": pending_count,
                "auto_refresh_enabled": True,
                "market_session": _market_session(now),
                "refresh_window": "24 小時分流排程 Asia/Taipei",
                "next_auto_refresh_at": next_auto_refresh_at,
                "last_refresh_finished_at": self._last_refresh_finished_at,
                "last_close_verification_at": last_close_verification_at,
                "channels": channels,
                "symbols": symbols,
            }

    async def _queue_all_channels(self, symbol: str, *, priority: int, force: bool) -> None:
        asset_type = await asyncio.to_thread(_asset_type_for_symbol, symbol)
        for channel in REFRESH_CHANNELS:
            categories = set(CHANNEL_CATEGORIES[channel])
            if asset_type == "ETF":
                categories -= {"CURRENT_PE", "EPS", "FINANCIAL_QUARTER", "MONTHLY_REVENUE", "PE_HISTORY"}
            if not categories:
                continue
            await self._enqueue_job(
                RefreshJob(
                    symbol=symbol,
                    channel=channel,
                    categories=frozenset(categories),
                    priority=priority,
                    force_full=force,
                )
            )

    async def _enqueue_job(self, job: RefreshJob) -> None:
        queue = self._queues.get(job.channel)
        if queue is None:
            return
        key = (job.channel, job.symbol)
        queued_job: RefreshJob | None = None
        async with self._lock:
            if job.symbol in self._deleted_symbols:
                return
            existing = self._pending_jobs.get(key)
            if existing:
                queued_job = _merge_refresh_jobs(existing, job)
                self._pending_jobs[key] = queued_job
            elif key in self._running_jobs:
                existing_follow_up = self._follow_up_jobs.get(key)
                self._follow_up_jobs[key] = _merge_refresh_jobs(existing_follow_up, job) if existing_follow_up else job
                return
            else:
                queued_job = job
                self._pending_jobs[key] = job
            self._sequence += 1
            sequence = self._sequence
            self._channel_runtime[job.channel].next_run_at = datetime.now(UTC)
            state = self._states.get(job.symbol) or RefreshSymbolState(job.symbol, "queued")
            state.status = "queued"
            state.queued_at = state.queued_at or datetime.now(UTC)
            state.message = f"Queued {job.channel.lower()} refresh."
            self._states[job.symbol] = state
        await queue.put((queued_job.priority, sequence, queued_job))
        async with self._db_write_lock:
            await asyncio.to_thread(_mark_job_sync_status, queued_job, "queued", datetime.now(UTC))

    async def _consume_channel(self, channel: str) -> None:
        queue = self._queues[channel]
        while True:
            _, _, job = await queue.get()
            try:
                key = (channel, job.symbol)
                async with self._lock:
                    if self._pending_jobs.get(key) is not job:
                        continue
                    self._pending_jobs.pop(key, None)
                    if job.symbol in self._deleted_symbols:
                        continue
                    self._running_jobs[key] = job
                    runtime = self._channel_runtime[channel]
                    runtime.current_symbols.add(job.symbol)
                    state = self._states.get(job.symbol) or RefreshSymbolState(job.symbol, "refreshing")
                    state.status = "refreshing"
                    state.started_at = datetime.now(UTC)
                    state.message = f"Refreshing {channel.lower()} data."
                    self._states[job.symbol] = state
                await self._execute_job(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._recover_unexpected_job_failure(job, exc)
            finally:
                queue.task_done()

    async def _execute_job(self, job: RefreshJob) -> None:
        started_at = datetime.now(UTC)
        async with self._db_write_lock:
            await asyncio.to_thread(_mark_job_sync_status, job, "running", started_at)
            await asyncio.to_thread(_record_channel_running, job, started_at)

        semaphore = self._quote_semaphore
        if job.channel in {CHANNEL_FUNDAMENTALS, CHANNEL_HISTORY}:
            semaphore = self._finmind_semaphore
        elif job.channel == CHANNEL_BROKER:
            semaphore = self._yahoo_semaphore

        payload: dict = {"results": {}, "errors": {}}
        try:
            fetch_coro = asyncio.to_thread(
                _fetch_channel_payload,
                job,
                self.finmind_token,
            )
            timeout_seconds = self.channel_timeout_seconds[job.channel]
            try:
                if semaphore:
                    async with semaphore:
                        payload = await asyncio.wait_for(fetch_coro, timeout=timeout_seconds)
                else:
                    payload = await asyncio.wait_for(fetch_coro, timeout=timeout_seconds)
            except TimeoutError as exc:
                raise TimeoutError(
                    f"{job.channel} refresh timed out after {timeout_seconds:g} seconds."
                ) from exc

            async with self._db_write_lock:
                await asyncio.to_thread(_apply_channel_payload, job, payload, started_at)
                await asyncio.to_thread(_record_job_errors, job, payload.get("errors", {}), started_at)
        except Exception as exc:
            payload.setdefault("errors", {})
            for category in job.categories:
                payload["errors"].setdefault(category, exc)
            async with self._db_write_lock:
                await asyncio.to_thread(_record_job_errors, job, payload["errors"], started_at)

        finished_at = datetime.now(UTC)
        failed_categories = sorted(payload.get("errors", {}))
        success_categories = sorted(set(job.categories) - set(failed_categories))
        message = f"{job.channel} completed: {', '.join(success_categories) or 'no successful categories'}"
        if failed_categories:
            message += f"; failed: {', '.join(failed_categories)}"
        async with self._db_write_lock:
            if job.channel == CHANNEL_QUOTE and failed_categories:
                first_error = next(iter(payload["errors"].values()))
                await asyncio.to_thread(_record_refresh_failed, job.symbol, str(first_error), started_at, finished_at)
            else:
                await asyncio.to_thread(_record_refresh_success, job.symbol, message, started_at, finished_at)

        follow_up = await self._finish_running_job(
            job,
            status="failed" if job.channel == CHANNEL_QUOTE and failed_categories else "success",
            message=message,
            started_at=started_at,
            finished_at=finished_at,
        )
        if follow_up:
            await self._enqueue_job(follow_up)
        if job.channel == CHANNEL_QUOTE and job.profile_required and not failed_categories:
            await self._queue_all_channels(job.symbol, priority=PRIORITY_MANUAL, force=True)

    async def _recover_unexpected_job_failure(self, job: RefreshJob, error: Exception) -> None:
        key = (job.channel, job.symbol)
        async with self._lock:
            state = self._states.get(job.symbol)
            started_at = state.started_at if state and state.started_at else datetime.now(UTC)
            still_running = key in self._running_jobs
        if not still_running:
            return

        finished_at = datetime.now(UTC)
        try:
            async with self._db_write_lock:
                await asyncio.to_thread(
                    _record_job_errors,
                    job,
                    {category: error for category in job.categories},
                    started_at,
                )
                if job.channel == CHANNEL_QUOTE:
                    await asyncio.to_thread(
                        _record_refresh_failed,
                        job.symbol,
                        str(error),
                        started_at,
                        finished_at,
                    )
        except Exception:
            # Runtime cleanup must still happen when SQLite error reporting
            # itself fails; otherwise the symbol remains stuck forever.
            pass

        message = f"{job.channel} refresh failed unexpectedly: {error}"
        follow_up = await self._finish_running_job(
            job,
            status="failed",
            message=message,
            started_at=started_at,
            finished_at=finished_at,
        )
        if follow_up and not (self._stop_event and self._stop_event.is_set()):
            await self._enqueue_job(follow_up)

    async def _finish_running_job(
        self,
        job: RefreshJob,
        *,
        status: str,
        message: str,
        started_at: datetime,
        finished_at: datetime,
    ) -> RefreshJob | None:
        async with self._lock:
            key = (job.channel, job.symbol)
            if self._running_jobs.pop(key, None) is None:
                return None
            runtime = self._channel_runtime[job.channel]
            runtime.current_symbols.discard(job.symbol)
            runtime.last_finished_at = finished_at
            self._last_refresh_finished_at = finished_at
            if job.symbol in self._deleted_symbols:
                self._follow_up_jobs.pop(key, None)
                return None
            self._states[job.symbol] = RefreshSymbolState(
                symbol=job.symbol,
                status=status,
                message=message,
                started_at=started_at,
                finished_at=finished_at,
            )
            return self._follow_up_jobs.pop(key, None)

    async def _run_scheduler(self) -> None:
        if not self._stop_event:
            return
        while True:
            now = datetime.now(UTC)
            try:
                if self._next_quote_at is None or now >= self._next_quote_at:
                    symbols = await asyncio.to_thread(_active_symbols)
                    for symbol in symbols:
                        await self._enqueue_job(
                            RefreshJob(symbol, CHANNEL_QUOTE, frozenset(("QUOTE",)), PRIORITY_AUTO)
                        )
                    interval = (
                        self.quote_market_interval_seconds
                        if _stock_market_is_open(now)
                        else self.quote_off_hours_interval_seconds
                    )
                    self._next_quote_at = now + timedelta(seconds=interval)
                    self._channel_runtime[CHANNEL_QUOTE].next_run_at = self._next_quote_at

                if self._next_schedule_scan_at is None or now >= self._next_schedule_scan_at:
                    scheduled = await asyncio.to_thread(
                        _scheduled_jobs_for_all,
                        now,
                        self.pe_poll_interval_seconds,
                        self.monthly_revenue_release_interval_seconds,
                    )
                    for symbol, channel, categories, is_retry in scheduled:
                        await self._enqueue_job(
                            RefreshJob(
                                symbol,
                                channel,
                                frozenset(categories),
                                PRIORITY_RETRY if is_retry else PRIORITY_AUTO,
                            )
                        )
                    self._next_schedule_scan_at = now + timedelta(minutes=1)
                    for channel in (CHANNEL_FUNDAMENTALS, CHANNEL_BROKER, CHANNEL_HISTORY):
                        self._channel_runtime[channel].next_run_at = self._next_schedule_scan_at
            except asyncio.CancelledError:
                raise
            except Exception:
                retry_at = datetime.now(UTC) + timedelta(seconds=SCHEDULER_TICK_SECONDS)
                if self._next_quote_at is None or self._next_quote_at <= now:
                    self._next_quote_at = retry_at
                    self._channel_runtime[CHANNEL_QUOTE].next_run_at = retry_at
                if self._next_schedule_scan_at is None or self._next_schedule_scan_at <= now:
                    self._next_schedule_scan_at = retry_at

            next_times = [value for value in (self._next_quote_at, self._next_schedule_scan_at) if value]
            next_auto = min(next_times) if next_times else now + timedelta(seconds=SCHEDULER_TICK_SECONDS)
            try:
                timeout = max(0.25, min(SCHEDULER_TICK_SECONDS, (next_auto - now).total_seconds()))
                await asyncio.wait_for(self._stop_event.wait(), timeout=timeout)
                return
            except TimeoutError:
                continue

    async def _run_futures_ticker(self) -> None:
        if not self._stop_event:
            return
        while True:
            now = datetime.now(UTC)
            active_session = current_futures_session(now).session_type != "closed"
            if active_session:
                await asyncio.to_thread(refresh_wtx_futures_cache)
            timeout = self.futures_refresh_seconds if active_session else 30
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=timeout)
                return
            except TimeoutError:
                continue


def _merge_refresh_jobs(existing: RefreshJob, incoming: RefreshJob) -> RefreshJob:
    return RefreshJob(
        symbol=existing.symbol,
        channel=existing.channel,
        categories=frozenset(set(existing.categories) | set(incoming.categories)),
        priority=min(existing.priority, incoming.priority),
        force_full=existing.force_full or incoming.force_full,
        profile_required=existing.profile_required or incoming.profile_required,
    )


def _asset_type_for_symbol(symbol: str) -> str:
    with SessionLocal() as session:
        return session.scalar(select(Stock.asset_type).where(Stock.symbol == symbol)) or "STOCK"


def _mark_job_sync_status(job: RefreshJob, status: str, at: datetime) -> None:
    for category in job.categories:
        mark_quality_sync_status(job.symbol, category, status, at)


def _record_channel_running(job: RefreshJob, started_at: datetime) -> None:
    with SessionLocal() as session:
        if not _active_stock_exists_in_session(session, job.symbol):
            return
        state = _get_or_create_refresh_state(session, job.symbol)
        state.status = "running"
        state.message = f"Refreshing {job.channel.lower()} data."
        state.started_at = started_at
        state.updated_at = started_at
        session.commit()


def _fetch_channel_payload(job: RefreshJob, finmind_token: str | None) -> dict:
    from .channels import fetch_channel_payload

    return fetch_channel_payload(job, finmind_token)


def _apply_channel_payload(job: RefreshJob, payload: dict, started_at: datetime) -> None:
    results = payload.get("results", {})
    if not results:
        return
    calculated_at = datetime.now(UTC)
    with SessionLocal() as session:
        stock = session.scalar(
            select(Stock).where(Stock.symbol == job.symbol, Stock.is_active.is_(True))
        )
        if not stock:
            return

        if "QUOTE" in results:
            profile, quote = results["QUOTE"]
            stock = apply_quote_refresh(
                session,
                profile=profile,
                quote=quote,
                calculated_at=calculated_at,
            )
            record_quality_success(
                session,
                stock,
                "QUOTE",
                attempted_at=started_at,
                data_date=_to_taipei(quote.price_updated_at).date(),
                fetched_at=calculated_at,
                source=quote.source,
            )

        fundamental_results = {key for key in results if key in CHANNEL_CATEGORIES[CHANNEL_FUNDAMENTALS]}
        if fundamental_results:
            pe_snapshot = results.get("CURRENT_PE")
            eps_rows = results.get("EPS")
            stock = apply_fundamental_refresh(
                session,
                symbol=job.symbol,
                current_pe=pe_snapshot.current_pe if pe_snapshot else None,
                pe_received=pe_snapshot is not None,
                pe_updated_at=calculated_at if pe_snapshot else None,
                pe_data_date=pe_snapshot.trade_date if pe_snapshot else None,
                pe_source=pe_snapshot.source if pe_snapshot else None,
                eps_rows=eps_rows,
                eps_updated_at=calculated_at if eps_rows is not None else None,
                calculated_at=calculated_at,
            )
            if pe_snapshot:
                record_quality_success(
                    session,
                    stock,
                    "CURRENT_PE",
                    attempted_at=started_at,
                    data_date=pe_snapshot.trade_date,
                    fetched_at=calculated_at,
                    source=pe_snapshot.source,
                )
            if eps_rows is not None:
                eps_period = next(
                    (row.eps_period.split("+")[0].strip() for row in eps_rows if row.eps_type == "TTM"),
                    None,
                )
                record_quality_success(
                    session,
                    stock,
                    "EPS",
                    attempted_at=started_at,
                    data_period=eps_period,
                    fetched_at=calculated_at,
                    source="FinMind TaiwanStockFinancialStatements",
                )

        if "FINANCIAL_QUARTER" in results:
            quarters = results["FINANCIAL_QUARTER"]
            apply_financial_quarter_snapshots(session, stock, quarters)
            if quarters:
                latest = max(quarters, key=lambda row: row.quarter_date)
                quarter = ((latest.quarter_date.month - 1) // 3) + 1
                record_quality_success(
                    session,
                    stock,
                    "FINANCIAL_QUARTER",
                    attempted_at=started_at,
                    data_period=f"{latest.quarter_date.year}Q{quarter}",
                    fetched_at=latest.fetched_at,
                    source=latest.source,
                )

        if "MONTHLY_REVENUE" in results:
            revenues = results["MONTHLY_REVENUE"]
            apply_monthly_revenue_snapshots(session, stock, revenues)
            if revenues:
                latest = max(revenues, key=lambda row: row.month_date)
                record_quality_success(
                    session,
                    stock,
                    "MONTHLY_REVENUE",
                    attempted_at=started_at,
                    data_period=latest.month_date.strftime("%Y-%m"),
                    fetched_at=latest.fetched_at,
                    source=latest.source,
                )

        if "BROKER_TRADING" in results:
            broker = results["BROKER_TRADING"]
            apply_broker_trading_snapshot(session, stock, broker)
            try:
                broker_date = date.fromisoformat(broker.trade_date.replace("/", "-"))
            except ValueError:
                broker_date = None
            record_quality_success(
                session,
                stock,
                "BROKER_TRADING",
                attempted_at=started_at,
                data_date=broker_date,
                fetched_at=broker.fetched_at,
                source=broker.source,
            )

        if "TECHNICAL_DAILY" in results:
            daily = results["TECHNICAL_DAILY"]
            apply_daily_price_snapshots(session, stock, daily)
            if daily:
                latest = max(daily, key=lambda row: row.trade_date)
                record_quality_success(
                    session,
                    stock,
                    "TECHNICAL_DAILY",
                    attempted_at=started_at,
                    data_date=latest.trade_date,
                    fetched_at=latest.fetched_at,
                    source=latest.source,
                )

        if "PE_HISTORY" in results:
            history = results["PE_HISTORY"]
            apply_pe_history_snapshots(session, stock, history)
            if history:
                latest = max(history, key=lambda row: row.trade_date)
                record_quality_success(
                    session,
                    stock,
                    "PE_HISTORY",
                    attempted_at=started_at,
                    data_date=latest.trade_date,
                    fetched_at=latest.fetched_at,
                    source=latest.source,
                )

        log_crawler_result(
            session,
            f"channel_refresh:{job.symbol}:{job.channel.lower()}",
            "SUCCESS",
            f"Updated categories: {', '.join(sorted(results))}",
            started_at,
        )
        session.commit()


def _record_job_errors(job: RefreshJob, errors: dict[str, Exception], started_at: datetime) -> None:
    for category, error in errors.items():
        record_quality_failure(job.symbol, category, error, attempted_at=started_at)


def _state_ready_for_attempt(
    state: StockDataQualityState | None,
    now: datetime,
    *,
    minimum_interval_seconds: int = 0,
) -> bool:
    if state and state.next_retry_at and _as_aware_utc(state.next_retry_at) > _as_aware_utc(now):
        return False
    if state and state.last_attempt_at and minimum_interval_seconds:
        elapsed = _as_aware_utc(now) - _as_aware_utc(state.last_attempt_at)
        if elapsed < timedelta(seconds=minimum_interval_seconds):
            return False
    return True


def _state_is_retry(state: StockDataQualityState | None, now: datetime) -> bool:
    return bool(
        state
        and (state.failure_count or 0) > 0
        and (state.next_retry_at is None or _as_aware_utc(state.next_retry_at) <= _as_aware_utc(now))
    )


def _scheduled_channel_categories(
    symbol: str,
    now: datetime,
    pe_poll_interval_seconds: int,
    revenue_release_interval_seconds: int,
) -> dict[str, set[str]]:
    result = {channel: set() for channel in REFRESH_CHANNELS}
    local = _to_taipei(now)
    target_trade_date = _expected_official_trade_date(now)
    with SessionLocal() as session:
        stock = session.scalar(select(Stock).where(Stock.symbol == symbol, Stock.is_active.is_(True)))
        if not stock:
            return result
        states = {
            row.category: row
            for row in session.scalars(
                select(StockDataQualityState).where(StockDataQualityState.stock_id == stock.id)
            ).all()
        }

        latest_metric = session.scalar(
            select(StockMetric)
            .where(StockMetric.stock_id == stock.id)
            .order_by(StockMetric.created_at.desc())
            .limit(1)
        )
        if stock.asset_type != "ETF":
            pe_state = states.get("CURRENT_PE")
            pe_date = latest_metric.pe_data_date if latest_metric else None
            if (pe_date is None or pe_date < target_trade_date) and _state_ready_for_attempt(
                pe_state,
                now,
                minimum_interval_seconds=pe_poll_interval_seconds,
            ):
                result[CHANNEL_FUNDAMENTALS].add("CURRENT_PE")

            pe_history_state = states.get("PE_HISTORY")
            pe_history_date = session.scalar(
                select(StockPEHistory.trade_date)
                .where(StockPEHistory.stock_id == stock.id)
                .order_by(StockPEHistory.trade_date.desc())
                .limit(1)
            )
            if (pe_history_date is None or pe_history_date < target_trade_date) and _state_ready_for_attempt(
                pe_history_state,
                now,
                minimum_interval_seconds=pe_poll_interval_seconds,
            ):
                result[CHANNEL_HISTORY].add("PE_HISTORY")

        daily_state = states.get("TECHNICAL_DAILY")
        daily_date = session.scalar(
            select(StockDailyPrice.trade_date)
            .where(StockDailyPrice.stock_id == stock.id)
            .order_by(StockDailyPrice.trade_date.desc())
            .limit(1)
        )
        daily_retry = _state_is_retry(daily_state, now)
        daily_window = local.weekday() < 5 and local.time() >= HISTORY_REFRESH_TIME
        if (daily_date is None or daily_date < target_trade_date) and (daily_window or daily_retry or local.weekday() >= 5):
            if _state_ready_for_attempt(daily_state, now, minimum_interval_seconds=60 if daily_retry else pe_poll_interval_seconds):
                result[CHANNEL_HISTORY].add("TECHNICAL_DAILY")

        broker_state = states.get("BROKER_TRADING")
        broker_row = session.scalar(
            select(StockBrokerTrading).where(StockBrokerTrading.stock_id == stock.id).limit(1)
        )
        try:
            broker_date = date.fromisoformat(broker_row.trade_date.replace("/", "-")) if broker_row else None
        except ValueError:
            broker_date = None
        broker_retry = _state_is_retry(broker_state, now)
        broker_window = local.weekday() < 5 and local.time() >= BROKER_REFRESH_TIME
        if (broker_date is None or broker_date < target_trade_date) and (broker_window or broker_retry or local.weekday() >= 5):
            if _state_ready_for_attempt(broker_state, now, minimum_interval_seconds=60 if broker_retry else 86400):
                result[CHANNEL_BROKER].add("BROKER_TRADING")

        if stock.asset_type != "ETF":
            eps_state = states.get("EPS")
            quarter_state = states.get("FINANCIAL_QUARTER")
            bundle_retry = _state_is_retry(eps_state, now) or _state_is_retry(quarter_state, now)
            bundle_window = local.weekday() < 5 and local.time() >= FUNDAMENTAL_REFRESH_TIME
            eps_fetched_at = session.scalar(
                select(StockEPS.eps_updated_at)
                .where(StockEPS.stock_id == stock.id)
                .order_by(StockEPS.eps_updated_at.desc())
                .limit(1)
            )
            quarter_fetched_at = session.scalar(
                select(StockFinancialQuarter.fetched_at)
                .where(StockFinancialQuarter.stock_id == stock.id)
                .order_by(StockFinancialQuarter.fetched_at.desc())
                .limit(1)
            )
            bundle_missing = eps_fetched_at is None or quarter_fetched_at is None
            bundle_not_today = not _is_same_day(eps_fetched_at, now) or not _is_same_day(quarter_fetched_at, now)
            if bundle_retry or bundle_missing or (bundle_window and bundle_not_today):
                if _state_ready_for_attempt(eps_state, now) and _state_ready_for_attempt(quarter_state, now):
                    result[CHANNEL_FUNDAMENTALS].update(("EPS", "FINANCIAL_QUARTER"))

            revenue_state = states.get("MONTHLY_REVENUE")
            latest_revenue = session.scalar(
                select(StockMonthlyRevenue)
                .where(StockMonthlyRevenue.stock_id == stock.id)
                .order_by(StockMonthlyRevenue.month_date.desc())
                .limit(1)
            )
            revenue_retry = _state_is_retry(revenue_state, now)
            release_window = 8 <= local.day <= 12 and time(9, 0) <= local.time() < time(23, 0)
            expected_month = _previous_month_period(now)
            missing_expected_month = latest_revenue is None or latest_revenue.month_date.strftime("%Y-%m") < expected_month
            release_due = release_window and missing_expected_month and _state_ready_for_attempt(
                revenue_state,
                now,
                minimum_interval_seconds=revenue_release_interval_seconds,
            )
            daily_due = (
                local.weekday() < 5
                and local.time() >= FUNDAMENTAL_REFRESH_TIME
                and (latest_revenue is None or not _is_same_day(latest_revenue.fetched_at, now))
            )
            if revenue_retry or release_due or daily_due:
                if _state_ready_for_attempt(revenue_state, now):
                    result[CHANNEL_FUNDAMENTALS].add("MONTHLY_REVENUE")

        for channel in result:
            result[channel] = {
                category
                for category in result[channel]
                if quality_retry_due(symbol, category, now)
            }
    return result


def _scheduled_jobs_for_all(
    now: datetime,
    pe_poll_interval_seconds: int,
    revenue_release_interval_seconds: int,
) -> list[tuple[str, str, set[str], bool]]:
    jobs = []
    for symbol in _active_symbols():
        categories_by_channel = _scheduled_channel_categories(
            symbol,
            now,
            pe_poll_interval_seconds,
            revenue_release_interval_seconds,
        )
        for channel, categories in categories_by_channel.items():
            if not categories or channel == CHANNEL_QUOTE:
                continue
            is_retry = _categories_include_retry(symbol, categories, now)
            jobs.append((symbol, channel, categories, is_retry))
    return jobs


def _categories_include_retry(symbol: str, categories: set[str], now: datetime) -> bool:
    with SessionLocal() as session:
        stock_id = session.scalar(select(Stock.id).where(Stock.symbol == symbol))
        if not stock_id:
            return False
        states = session.scalars(
            select(StockDataQualityState).where(
                StockDataQualityState.stock_id == stock_id,
                StockDataQualityState.category.in_(categories),
            )
        ).all()
        return any(_state_is_retry(state, now) for state in states)


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


def _refresh_state_response(state) -> dict:
    if isinstance(state, dict):
        return state
    return {
        "symbol": state.symbol,
        "status": state.status,
        "message": state.message,
        "failure_count": getattr(state, "failure_count", 0) or 0,
        "last_error": getattr(state, "last_error", None),
        "next_retry_at": getattr(state, "next_retry_at", None),
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
    mark_quality_sync_status(symbol, "QUOTE", "queued", queued_at)


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
    mark_quality_sync_status(symbol, "QUOTE", "running", started_at)


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
    if not _is_weekday(now) or _to_taipei(now).time() < MARKET_CLOSE_VERIFICATION_TIME:
        return False

    last_finished_at = _last_close_verification_at()
    return not (
        _is_same_day(last_finished_at, now)
        and _to_taipei(last_finished_at).time() >= MARKET_CLOSE_VERIFICATION_TIME
    )


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
        pe_due = latest_metric is None or not _is_same_refresh_day(latest_metric.pe_updated_at, now)
        if not pe_due and latest_metric is not None:
            pe_due = _stale_pe_retry_due(
                latest_metric.pe_data_date,
                latest_metric.pe_updated_at,
                now,
            )
        return (
            pe_due,
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


def _pe_history_due(symbol: str, now: datetime) -> bool:
    with SessionLocal() as session:
        stock = session.scalar(select(Stock).where(Stock.symbol == symbol))
        if not stock:
            return False

        latest = session.scalar(
            select(StockPEHistory)
            .where(StockPEHistory.stock_id == stock.id)
            .order_by(StockPEHistory.trade_date.desc())
            .limit(1)
        )
        if latest is None:
            return True
        if not _is_same_day(latest.fetched_at, now):
            return True
        return _stale_pe_retry_due(latest.trade_date, latest.fetched_at, now)


def _expected_latest_pe_trade_date(now: datetime) -> date:
    return _expected_official_trade_date(now)


def _stale_pe_retry_due(pe_data_date, last_attempt_at: datetime | None, now: datetime) -> bool:
    expected_date = _expected_latest_pe_trade_date(now)
    if pe_data_date is not None and pe_data_date >= expected_date:
        return False
    if last_attempt_at is None:
        return True
    return _as_aware_utc(now) - _as_aware_utc(last_attempt_at) >= STALE_PE_RETRY_INTERVAL


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
    pe_due = pe_due and quality_retry_due(symbol, "CURRENT_PE", started_at, force=force_full)
    eps_due = eps_due and quality_retry_due(symbol, "EPS", started_at, force=force_full)
    broker_trading_due = (True if force_full else _broker_trading_due(symbol, started_at)) and quality_retry_due(symbol, "BROKER_TRADING", started_at, force=force_full)
    daily_prices_due = (True if force_full else _daily_prices_due(symbol, started_at)) and quality_retry_due(symbol, "TECHNICAL_DAILY", started_at, force=force_full)
    pe_history_due = (True if force_full else _pe_history_due(symbol, started_at)) and quality_retry_due(symbol, "PE_HISTORY", started_at, force=force_full)
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
    monthly_revenue_due = monthly_revenue_due and quality_retry_due(symbol, "MONTHLY_REVENUE", started_at, force=force_full)
    financial_quarters_due = financial_quarters_due and quality_retry_due(symbol, "FINANCIAL_QUARTER", started_at, force=force_full)
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

    try:
        quote = fetch_stock_quote(symbol, profile=profile, finmind_token=finmind_token)
    except Exception as exc:
        record_quality_failure(symbol, "QUOTE", exc, attempted_at=started_at)
        raise
    source_parts.append(quote.source)
    current_pe = None
    pe_data_date = None
    pe_updated_at = None
    pe_source = None
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
            pe_snapshot = fetch_stock_pe_snapshot(
                symbol,
                finmind_token=finmind_token,
                end_date=started_at.astimezone(TAIPEI_TZ).date(),
            )
        except Exception as exc:
            messages.append(f"PE 更新失敗，沿用快取：{exc}")
            source_parts.append("cached PE")
            record_quality_failure(symbol, "CURRENT_PE", exc, attempted_at=started_at)
        else:
            current_pe = pe_snapshot.current_pe
            pe_data_date = pe_snapshot.trade_date
            pe_updated_at = calculated_at
            if current_pe is None and pe_data_date is None:
                messages.append("TWSE/FinMind PE 暫無資料，沿用快取")
                source_parts.append("cached PE")
                pe_updated_at = None
            else:
                data_date_label = pe_data_date.isoformat() if pe_data_date else "最新日期"
                messages.append(f"PE 已更新至 {data_date_label}")
                pe_source = pe_snapshot.source
                source_parts.append(pe_snapshot.source)
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
            record_quality_failure(symbol, "EPS", exc, attempted_at=started_at)
        else:
            eps_updated_at = calculated_at
            messages.append("EPS 已按日更新")
            source_parts.append("FinMind financial EPS")
    else:
        messages.append("EPS 沿用今日快取")
        source_parts.append("cached daily EPS")

    if current_pe is None and eps_rows:
        derived_pe = derive_pe(quote.current_price, eps_rows)
        if derived_pe is not None:
            current_pe = derived_pe
            pe_updated_at = calculated_at
            pe_source = "derived PE"
            messages.append("PE 已由最新 EPS 推算")
            source_parts.append("derived PE")
        else:
            messages.append("PE 不適用，略過估值")
            source_parts.append("PE not applicable")

    if broker_trading_due:
        try:
            broker_trading = fetch_broker_trading(symbol)
        except Exception as exc:
            messages.append(f"主力進出更新失敗，沿用快取：{exc}")
            record_quality_failure(symbol, "BROKER_TRADING", exc, attempted_at=started_at)
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
            record_quality_failure(symbol, "TECHNICAL_DAILY", exc, attempted_at=started_at)
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
            record_quality_failure(symbol, "PE_HISTORY", exc, attempted_at=started_at)
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
            record_quality_failure(symbol, "MONTHLY_REVENUE", exc, attempted_at=started_at)
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
            record_quality_failure(symbol, "FINANCIAL_QUARTER", exc, attempted_at=started_at)
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
                pe_data_date=pe_data_date,
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
            record_quality_success(
                session,
                stock,
                "QUOTE",
                attempted_at=started_at,
                data_date=_to_taipei(quote.price_updated_at).date(),
                fetched_at=calculated_at,
                source=quote.source,
            )
            if profile.asset_type != "ETF" and current_pe is not None:
                record_quality_success(
                    session,
                    stock,
                    "CURRENT_PE",
                    attempted_at=started_at,
                    data_date=pe_data_date or _to_taipei(quote.price_updated_at).date(),
                    fetched_at=pe_updated_at or calculated_at,
                    source=pe_source or "derived PE",
                )
            if profile.asset_type != "ETF" and eps_rows is not None:
                eps_period = next((row.eps_period.split("+")[0].strip() for row in eps_rows if row.eps_type == "TTM"), None)
                record_quality_success(
                    session,
                    stock,
                    "EPS",
                    attempted_at=started_at,
                    data_period=eps_period,
                    fetched_at=eps_updated_at or calculated_at,
                    source="FinMind TaiwanStockFinancialStatements",
                )
            if broker_trading:
                try:
                    broker_date = date.fromisoformat(broker_trading.trade_date.replace("/", "-"))
                except ValueError:
                    broker_date = None
                record_quality_success(
                    session,
                    stock,
                    "BROKER_TRADING",
                    attempted_at=started_at,
                    data_date=broker_date,
                    fetched_at=broker_trading.fetched_at,
                    source=broker_trading.source,
                )
            if daily_prices:
                latest_daily = max(daily_prices, key=lambda row: row.trade_date)
                record_quality_success(
                    session,
                    stock,
                    "TECHNICAL_DAILY",
                    attempted_at=started_at,
                    data_date=latest_daily.trade_date,
                    fetched_at=latest_daily.fetched_at,
                    source=latest_daily.source,
                )
            if pe_history:
                latest_pe = max(pe_history, key=lambda row: row.trade_date)
                record_quality_success(
                    session,
                    stock,
                    "PE_HISTORY",
                    attempted_at=started_at,
                    data_date=latest_pe.trade_date,
                    fetched_at=latest_pe.fetched_at,
                    source=latest_pe.source,
                )
            if monthly_revenues:
                latest_revenue = max(monthly_revenues, key=lambda row: row.month_date)
                record_quality_success(
                    session,
                    stock,
                    "MONTHLY_REVENUE",
                    attempted_at=started_at,
                    data_period=latest_revenue.month_date.strftime("%Y-%m"),
                    fetched_at=latest_revenue.fetched_at,
                    source=latest_revenue.source,
                )
            if financial_quarters:
                latest_quarter = max(financial_quarters, key=lambda row: row.quarter_date)
                record_quality_success(
                    session,
                    stock,
                    "FINANCIAL_QUARTER",
                    attempted_at=started_at,
                    data_period=f"{latest_quarter.quarter_date.year}Q{((latest_quarter.quarter_date.month - 1) // 3) + 1}",
                    fetched_at=latest_quarter.fetched_at,
                    source=latest_quarter.source,
                )
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
