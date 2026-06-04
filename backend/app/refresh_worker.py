from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from .database import (
    SessionLocal,
    Stock,
    StockBrokerTrading,
    StockEPS,
    StockMetric,
    apply_broker_trading_snapshot,
    apply_layered_stock_refresh,
    log_crawler_result,
    next_display_order,
)
from .wantgoo import (
    derive_pe,
    fetch_stock_eps,
    fetch_stock_pe,
    fetch_stock_profile,
    fetch_stock_quote,
    normalize_symbol,
)
from .yahoo_broker import fetch_broker_trading


@dataclass
class RefreshSymbolState:
    symbol: str
    status: str
    message: str = ""
    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class BackgroundRefreshManager:
    def __init__(self, interval_seconds: int, wantgoo_base_url: str) -> None:
        self.interval_seconds = interval_seconds
        self.wantgoo_base_url = wantgoo_base_url
        self._queue: asyncio.Queue[str] | None = None
        self._queued_symbols: set[str] = set()
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
        await self.queue_active_stocks()

    async def stop(self) -> None:
        if self._stop_event:
            self._stop_event.set()

        tasks = [task for task in (self._consumer_task, self._ticker_task) if task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def queue_symbol(self, symbol: str, *, create_placeholder: bool = False) -> RefreshSymbolState:
        normalized_symbol = normalize_symbol(symbol)
        if create_placeholder:
            await asyncio.to_thread(_ensure_active_placeholder, normalized_symbol)

        now = datetime.now(UTC)
        async with self._lock:
            self._deleted_symbols.discard(normalized_symbol)
            existing = self._states.get(normalized_symbol)
            if normalized_symbol in self._queued_symbols:
                return existing or RefreshSymbolState(
                    symbol=normalized_symbol,
                    status="queued",
                    queued_at=now,
                    message="Already queued.",
                )
            if normalized_symbol == self._current_symbol:
                return existing or RefreshSymbolState(
                    symbol=normalized_symbol,
                    status="refreshing",
                    queued_at=now,
                    started_at=now,
                    message="Already refreshing.",
                )

            state = RefreshSymbolState(
                symbol=normalized_symbol,
                status="queued",
                queued_at=now,
                message="Queued for background refresh.",
            )
            self._states[normalized_symbol] = state
            self._queued_symbols.add(normalized_symbol)

        await self._put_queue(normalized_symbol)
        return state

    async def forget_symbol(self, symbol: str) -> None:
        normalized_symbol = normalize_symbol(symbol)
        async with self._lock:
            self._deleted_symbols.add(normalized_symbol)
            self._queued_symbols.discard(normalized_symbol)
            self._states.pop(normalized_symbol, None)
            if self._current_symbol == normalized_symbol:
                self._current_symbol = None

    async def queue_active_stocks(self) -> list[RefreshSymbolState]:
        symbols = await asyncio.to_thread(_active_symbols)
        states = []
        for symbol in symbols:
            states.append(await self.queue_symbol(symbol))
        return states

    async def snapshot(self) -> dict:
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
                "next_auto_refresh_at": self._next_auto_refresh_at,
                "last_refresh_finished_at": self._last_refresh_finished_at,
                "symbols": [
                    {
                        "symbol": state.symbol,
                        "status": state.status,
                        "message": state.message,
                        "queued_at": state.queued_at,
                        "started_at": state.started_at,
                        "finished_at": state.finished_at,
                    }
                    for state in sorted(
                        self._states.values(),
                        key=lambda item: item.finished_at or item.started_at or item.queued_at or datetime.min.replace(tzinfo=UTC),
                        reverse=True,
                    )
                ],
            }

    async def _put_queue(self, symbol: str) -> None:
        if not self._queue:
            return
        await self._queue.put(symbol)

    async def _consume_queue(self) -> None:
        if not self._queue:
            return

        while True:
            symbol = await self._queue.get()
            try:
                await self._refresh_symbol(symbol)
            finally:
                self._queue.task_done()

    async def _run_ticker(self) -> None:
        if not self._stop_event:
            return

        while True:
            async with self._lock:
                self._next_auto_refresh_at = datetime.now(UTC) + timedelta(seconds=self.interval_seconds)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_seconds)
                return
            except TimeoutError:
                await self.queue_active_stocks()

    async def _refresh_symbol(self, symbol: str) -> None:
        started_at = datetime.now(UTC)
        async with self._lock:
            self._queued_symbols.discard(symbol)
            if symbol in self._deleted_symbols:
                self._states.pop(symbol, None)
                return

            self._current_symbol = symbol
            self._states[symbol] = RefreshSymbolState(
                symbol=symbol,
                status="refreshing",
                message="Refreshing market data.",
                queued_at=self._states.get(symbol, RefreshSymbolState(symbol, "queued")).queued_at,
                started_at=started_at,
            )

        try:
            message = await asyncio.to_thread(_refresh_symbol_sync, symbol, self.wantgoo_base_url, started_at)
        except Exception as exc:
            finished_at = datetime.now(UTC)
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


def _is_same_month(value: datetime | None, now: datetime) -> bool:
    if not value:
        return False
    return value.year == now.year and value.month == now.month


def _is_same_day(value: datetime | None, now: datetime) -> bool:
    if not value:
        return False
    return value.date() == now.date()


def _calendar_quarter(value: datetime) -> tuple[int, int]:
    return value.year, ((value.month - 1) // 3) + 1


def _is_same_quarter(value: datetime | None, now: datetime) -> bool:
    if not value:
        return False
    return _calendar_quarter(value) == _calendar_quarter(now)


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
            latest_metric is None or not _is_same_month(latest_metric.pe_updated_at, now),
            latest_eps_updated_at is None or not _is_same_quarter(latest_eps_updated_at, now),
        )


def _broker_trading_due(symbol: str, now: datetime) -> bool:
    with SessionLocal() as session:
        stock = session.scalar(select(Stock).where(Stock.symbol == symbol))
        if not stock or stock.asset_type == "ETF":
            return False

        latest_broker_trading = session.scalar(
            select(StockBrokerTrading)
            .where(StockBrokerTrading.stock_id == stock.id)
            .limit(1)
        )
        return latest_broker_trading is None or not _is_same_day(latest_broker_trading.fetched_at, now)


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


def _refresh_symbol_sync(symbol: str, wantgoo_base_url: str, started_at: datetime) -> str:
    if not _active_stock_exists(symbol):
        return "標的已刪除，略過背景更新"

    pe_due, eps_due = _refresh_due(symbol, started_at)
    broker_trading_due = _broker_trading_due(symbol, started_at)
    calculated_at = datetime.now(UTC)
    profile = fetch_stock_profile(symbol, wantgoo_base_url)
    quote = fetch_stock_quote(symbol, wantgoo_base_url)

    messages = ["股價已更新"]
    source_parts = ["WantGoo quote"]
    current_pe = None
    pe_updated_at = None
    eps_rows = None
    eps_updated_at = None
    broker_trading = None

    if profile.asset_type == "ETF":
        messages.append("ETF 僅更新現價")
    elif pe_due:
        try:
            current_pe = fetch_stock_pe(symbol)
        except Exception as exc:
            messages.append(f"PE 更新失敗，沿用快取：{exc}")
            source_parts.append("cached PE")
        else:
            if current_pe is None:
                messages.append("TWSE PE 暫無資料，沿用快取")
                source_parts.append("cached PE")
            else:
                pe_updated_at = calculated_at
                messages.append("PE 已按月更新")
                source_parts.append("TWSE monthly PE")
    else:
        messages.append("PE 沿用本月快取")
        source_parts.append("cached monthly PE")

    if profile.asset_type == "ETF":
        pass
    elif eps_due:
        try:
            eps_rows = fetch_stock_eps(symbol)
        except Exception as exc:
            messages.append(f"EPS 更新失敗，沿用快取：{exc}")
            source_parts.append("cached EPS")
        else:
            eps_updated_at = calculated_at
            messages.append("EPS 已按季更新")
            source_parts.append("FinMind quarterly EPS")
    else:
        messages.append("EPS 沿用本季快取")
        source_parts.append("cached quarterly EPS")

    if current_pe is None and eps_rows:
        current_pe = derive_pe(quote.current_price, eps_rows)
        pe_updated_at = calculated_at
        messages.append("PE 已由最新 EPS 推算")
        source_parts.append("derived PE")

    if profile.asset_type == "ETF":
        pass
    elif broker_trading_due:
        try:
            broker_trading = fetch_broker_trading(symbol)
        except Exception as exc:
            messages.append(f"主力進出更新失敗，沿用快取：{exc}")
        else:
            messages.append("主力進出已按日更新")
    else:
        messages.append("主力進出沿用今日快取")

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
            if broker_trading and stock.asset_type != "ETF":
                apply_broker_trading_snapshot(session, stock, broker_trading)
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
