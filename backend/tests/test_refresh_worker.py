from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.database import Base, Stock, StockMetric
import backend.app.refresh.manager as refresh_worker
from backend.app.refresh.manager import (
    BackgroundRefreshManager,
    CHANNEL_FUNDAMENTALS,
    PRIORITY_AUTO,
    PRIORITY_MANUAL,
    RefreshJob,
    _auto_refresh_enabled,
    _close_verification_due,
    _expected_official_trade_date,
    _expected_latest_pe_trade_date,
    _market_session,
    _merge_refresh_jobs,
    _next_auto_refresh_at,
    _stock_market_is_open,
    _stale_pe_retry_due,
)


class AutoRefreshScheduleTest(unittest.TestCase):
    def test_auto_refresh_is_always_enabled_but_reports_market_session(self) -> None:
        market_open = datetime(2026, 6, 22, 1, 30, tzinfo=UTC)
        off_hours = datetime(2026, 6, 22, 17, 0, tzinfo=UTC)
        weekend = datetime(2026, 6, 21, 4, 0, tzinfo=UTC)

        for sample in (market_open, off_hours, weekend):
            with self.subTest(sample=sample):
                self.assertTrue(_auto_refresh_enabled(sample))
        self.assertEqual(_market_session(market_open), "market_open")
        self.assertEqual(_market_session(off_hours), "off_hours")
        self.assertEqual(_market_session(weekend), "off_hours")
        self.assertTrue(_stock_market_is_open(market_open))
        self.assertFalse(_stock_market_is_open(off_hours))

    def test_next_auto_refresh_uses_interval_at_all_times(self) -> None:
        now = datetime(2026, 6, 21, 4, 0, tzinfo=UTC)

        self.assertEqual(_next_auto_refresh_at(now, 60), now + timedelta(seconds=60))


class CloseVerificationScheduleTest(unittest.TestCase):
    @patch("backend.app.refresh.manager._last_close_verification_at", return_value=None)
    def test_close_verification_waits_until_1800_taipei(self, _last_refresh) -> None:
        self.assertFalse(_close_verification_due(datetime(2026, 6, 22, 9, 59, tzinfo=UTC)))
        self.assertTrue(_close_verification_due(datetime(2026, 6, 22, 10, 0, tzinfo=UTC)))

    @patch("backend.app.refresh.manager._last_close_verification_at")
    def test_pre_1800_same_day_run_does_not_block_final_pass(self, last_refresh) -> None:
        last_refresh.return_value = datetime(2026, 6, 22, 6, 5, tzinfo=UTC)

        self.assertTrue(_close_verification_due(datetime(2026, 6, 22, 10, 0, tzinfo=UTC)))

    @patch("backend.app.refresh.manager._last_close_verification_at")
    def test_post_1800_same_day_run_is_not_repeated(self, last_refresh) -> None:
        last_refresh.return_value = datetime(2026, 6, 22, 10, 5, tzinfo=UTC)

        self.assertFalse(_close_verification_due(datetime(2026, 6, 22, 11, 0, tzinfo=UTC)))


class PERefreshDueTest(unittest.TestCase):
    def test_expected_latest_pe_trade_date_waits_until_1800_and_skips_weekend(self) -> None:
        self.assertEqual(
            _expected_latest_pe_trade_date(datetime(2026, 6, 29, 4, 0, tzinfo=UTC)),
            date(2026, 6, 26),
        )
        self.assertEqual(
            _expected_official_trade_date(datetime(2026, 6, 29, 10, 0, tzinfo=UTC)),
            date(2026, 6, 29),
        )
        self.assertEqual(
            _expected_latest_pe_trade_date(datetime(2026, 6, 28, 4, 0, tzinfo=UTC)),
            date(2026, 6, 26),
        )

    def test_stale_pe_retries_after_interval_until_data_date_catches_up(self) -> None:
        now = datetime(2026, 6, 29, 10, 0, tzinfo=UTC)

        self.assertFalse(
            _stale_pe_retry_due(
                date(2026, 6, 26),
                now - timedelta(minutes=10),
                now,
            )
        )
        self.assertTrue(
            _stale_pe_retry_due(
                date(2026, 6, 26),
                now - timedelta(minutes=15),
                now,
            )
        )
        self.assertFalse(
            _stale_pe_retry_due(
                date(2026, 6, 29),
                now - timedelta(hours=2),
                now,
            )
        )

    def test_refresh_due_retries_when_pe_data_date_lags_expected_trade_date(self) -> None:
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(engine)
        old_session_local = refresh_worker.SessionLocal
        now = datetime(2026, 6, 30, 11, 0, tzinfo=UTC)

        try:
            refresh_worker.SessionLocal = lambda: Session(engine)
            with Session(engine) as session:
                stock = Stock(symbol="4958", name="臻鼎-KY", asset_type="STOCK", market="TWSE")
                session.add(stock)
                session.flush()
                session.add(
                    StockMetric(
                        stock_id=stock.id,
                        current_price=Decimal("639.00"),
                        current_pe=Decimal("80.82"),
                        price_updated_at=now,
                        pe_updated_at=now - timedelta(minutes=16),
                        pe_data_date=date(2026, 6, 29),
                        source="test",
                    )
                )
                session.commit()

            pe_due, eps_due = refresh_worker._refresh_due("4958", now)
        finally:
            refresh_worker.SessionLocal = old_session_local
            engine.dispose()

        self.assertTrue(pe_due)
        self.assertTrue(eps_due)


class ChannelQueueTest(unittest.IsolatedAsyncioTestCase):
    def test_merge_job_upgrades_priority_force_and_categories(self) -> None:
        automatic = RefreshJob(
            "2330",
            CHANNEL_FUNDAMENTALS,
            frozenset(("CURRENT_PE",)),
            PRIORITY_AUTO,
        )
        manual = RefreshJob(
            "2330",
            CHANNEL_FUNDAMENTALS,
            frozenset(("EPS",)),
            PRIORITY_MANUAL,
            force_full=True,
        )

        merged = _merge_refresh_jobs(automatic, manual)

        self.assertEqual(merged.priority, PRIORITY_MANUAL)
        self.assertTrue(merged.force_full)
        self.assertEqual(merged.categories, frozenset(("CURRENT_PE", "EPS")))

    @patch("backend.app.refresh.manager._mark_job_sync_status")
    async def test_pending_job_is_deduplicated_and_upgraded(self, _mark_status) -> None:
        manager = BackgroundRefreshManager(60)
        manager._queues = {CHANNEL_FUNDAMENTALS: asyncio.PriorityQueue()}
        first = RefreshJob("2330", CHANNEL_FUNDAMENTALS, frozenset(("CURRENT_PE",)), PRIORITY_AUTO)
        second = RefreshJob(
            "2330",
            CHANNEL_FUNDAMENTALS,
            frozenset(("EPS",)),
            PRIORITY_MANUAL,
            force_full=True,
        )

        await manager._enqueue_job(first)
        await manager._enqueue_job(second)

        pending = manager._pending_jobs[(CHANNEL_FUNDAMENTALS, "2330")]
        self.assertEqual(len(manager._pending_jobs), 1)
        self.assertEqual(pending.categories, frozenset(("CURRENT_PE", "EPS")))
        self.assertEqual(pending.priority, PRIORITY_MANUAL)
        self.assertTrue(pending.force_full)

    @patch("backend.app.refresh.manager.refresh_wtx_futures_cache")
    @patch("backend.app.refresh.manager.current_futures_session")
    async def test_wtx_closed_session_makes_no_external_request(self, current_session, refresh_wtx) -> None:
        current_session.return_value = type("Session", (), {"session_type": "closed"})()
        manager = BackgroundRefreshManager(60, futures_refresh_seconds=10)
        manager._stop_event = asyncio.Event()

        task = asyncio.create_task(manager._run_futures_ticker())
        await asyncio.sleep(0)
        manager._stop_event.set()
        await task

        refresh_wtx.assert_not_called()

    @patch("backend.app.refresh.manager._mark_job_sync_status")
    async def test_slow_broker_consumer_does_not_block_quote_consumer(self, _mark_status) -> None:
        import backend.app.refresh.manager as worker

        manager = BackgroundRefreshManager(60)
        manager._queues = {
            worker.CHANNEL_QUOTE: asyncio.PriorityQueue(),
            worker.CHANNEL_BROKER: asyncio.PriorityQueue(),
        }
        broker_release = asyncio.Event()
        quote_finished = asyncio.Event()

        async def fake_execute(job) -> None:
            if job.channel == worker.CHANNEL_BROKER:
                await broker_release.wait()
            else:
                quote_finished.set()

        manager._execute_job = fake_execute
        consumers = [
            asyncio.create_task(manager._consume_channel(worker.CHANNEL_BROKER)),
            asyncio.create_task(manager._consume_channel(worker.CHANNEL_QUOTE)),
        ]
        try:
            await manager._enqueue_job(
                RefreshJob("2330", worker.CHANNEL_BROKER, frozenset(("BROKER_TRADING",)), PRIORITY_AUTO)
            )
            await manager._enqueue_job(
                RefreshJob("2330", worker.CHANNEL_QUOTE, frozenset(("QUOTE",)), PRIORITY_AUTO)
            )
            await asyncio.wait_for(quote_finished.wait(), timeout=0.2)
            self.assertFalse(broker_release.is_set())
        finally:
            broker_release.set()
            for consumer in consumers:
                consumer.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
