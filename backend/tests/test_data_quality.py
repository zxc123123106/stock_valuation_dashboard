from __future__ import annotations

import unittest
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.app.data_quality as data_quality
from backend.app.data_quality import (
    expected_month_period,
    expected_quarter_period,
    freshness_for_state,
    record_quality_failure,
    record_quality_success,
)
from backend.app.ai_analysis import AI_MODE_UNHELD, PROMPT_VERSION
from backend.app.database import Base, Stock, StockAIAnalysis, StockDailyPrice, StockDataQualityState
from backend.app.services.application import _ai_quality_components, _stock_data_quality_response


class DataQualityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, future=True)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _stock(self, session, *, asset_type="STOCK") -> Stock:
        stock = Stock(symbol="4958", name="臻鼎-KY", asset_type=asset_type, is_active=True)
        session.add(stock)
        session.commit()
        session.refresh(stock)
        return stock

    def _daily(self, session, stock, trade_date: date) -> None:
        session.add(
            StockDailyPrice(
                stock_id=stock.id,
                trade_date=trade_date,
                open_price=Decimal("100"),
                high_price=Decimal("105"),
                low_price=Decimal("99"),
                close_price=Decimal("103"),
                volume=1000,
                source="FinMind",
                fetched_at=datetime(2026, 7, 13, tzinfo=UTC),
            )
        )

    def test_quote_is_realtime_during_market_and_stale_after_thirty_minutes(self) -> None:
        now = datetime(2026, 7, 13, 2, 0, tzinfo=UTC)  # 10:00 Asia/Taipei
        with self.Session() as session:
            stock = self._stock(session)
            self._daily(session, stock, date(2026, 7, 13))
            state = StockDataQualityState(
                stock_id=stock.id,
                category="QUOTE",
                data_date=date(2026, 7, 13),
                fetched_at=now - timedelta(minutes=1),
                last_success_at=now - timedelta(minutes=1),
            )
            session.add(state)
            session.commit()
            self.assertEqual(freshness_for_state(session, state, "QUOTE", now=now), "REALTIME")
            state.fetched_at = now - timedelta(minutes=31)
            session.commit()
            self.assertEqual(freshness_for_state(session, state, "QUOTE", now=now), "STALE")

    def test_pe_uses_previous_trade_date_before_1800(self) -> None:
        with self.Session() as session:
            stock = self._stock(session)
            self._daily(session, stock, date(2026, 7, 10))
            self._daily(session, stock, date(2026, 7, 13))
            state = StockDataQualityState(
                stock_id=stock.id,
                category="CURRENT_PE",
                data_date=date(2026, 7, 10),
                fetched_at=datetime(2026, 7, 13, tzinfo=UTC),
            )
            session.add(state)
            session.commit()
            before_1800 = datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
            after_1800 = datetime(2026, 7, 13, 11, 0, tzinfo=UTC)
            self.assertEqual(freshness_for_state(session, state, "CURRENT_PE", now=before_1800), "CURRENT")
            self.assertEqual(freshness_for_state(session, state, "CURRENT_PE", now=after_1800), "DELAYED")

    def test_fundamental_expected_periods_include_publication_grace(self) -> None:
        self.assertEqual(expected_month_period(datetime(2026, 7, 13, tzinfo=UTC)), "2026-06")
        self.assertEqual(expected_quarter_period(datetime(2026, 5, 18, tzinfo=UTC)), "2026Q1")

    def test_failure_preserves_cache_and_success_clears_error(self) -> None:
        old_session_local = data_quality.SessionLocal
        data_quality.SessionLocal = lambda: self.Session()
        try:
            with self.Session() as session:
                stock = self._stock(session)
                record_quality_success(
                    session,
                    stock,
                    "BROKER_TRADING",
                    attempted_at=datetime(2026, 7, 13, tzinfo=UTC),
                    data_date=date(2026, 7, 11),
                    fetched_at=datetime(2026, 7, 13, tzinfo=UTC),
                    source="Yahoo",
                )
                session.commit()
            record_quality_failure(
                "4958",
                "BROKER_TRADING",
                "HTTP 429 Too Many Requests",
                attempted_at=datetime(2026, 7, 13, 1, tzinfo=UTC),
            )
            with self.Session() as session:
                stock = session.query(Stock).filter_by(symbol="4958").one()
                state = session.query(StockDataQualityState).filter_by(stock_id=stock.id, category="BROKER_TRADING").one()
                self.assertTrue(state.is_cached)
                self.assertEqual(state.last_error_summary, "資料來源已達請求上限")
                self.assertEqual(state.data_date, date(2026, 7, 11))
                record_quality_success(
                    session,
                    stock,
                    "BROKER_TRADING",
                    attempted_at=datetime(2026, 7, 13, 2, tzinfo=UTC),
                    data_date=date(2026, 7, 13),
                    fetched_at=datetime(2026, 7, 13, 2, tzinfo=UTC),
                    source="Yahoo",
                )
                session.commit()
                self.assertFalse(state.is_cached)
                self.assertIsNone(state.last_error_summary)
                self.assertEqual(state.failure_count, 0)
        finally:
            data_quality.SessionLocal = old_session_local

    def test_etf_pe_and_fundamental_are_not_applicable(self) -> None:
        with self.Session() as session:
            stock = self._stock(session, asset_type="ETF")
            response = _stock_data_quality_response(stock, session)
            items = {item.category: item for item in response.items}
            self.assertEqual(items["PE"].freshness_status, "NOT_APPLICABLE")
            self.assertEqual(items["FUNDAMENTAL"].freshness_status, "NOT_APPLICABLE")

    def test_successful_ai_analysis_stays_current_for_its_taipei_date(self) -> None:
        now = datetime(2026, 7, 13, 14, 10, tzinfo=UTC)
        requested_at = "2026-07-13T22:00:00+08:00"
        with self.Session() as session:
            stock = self._stock(session)
            success = StockAIAnalysis(
                stock_id=stock.id,
                provider="openrouter",
                model="test-model",
                analysis_mode=AI_MODE_UNHELD,
                prompt_version=PROMPT_VERSION,
                analysis_date=date(2026, 7, 13),
                input_hash="old-input-hash",
                request_payload_json=json.dumps(
                    {"analysis_context": {"analysis_requested_at": requested_at}}
                ),
                response_json="{}",
                validation_errors_json="[]",
                status="success",
                created_at=now - timedelta(minutes=10),
                updated_at=now - timedelta(minutes=9),
            )
            session.add(success)
            session.commit()

            component = _ai_quality_components(stock, session, now)[0]

            self.assertEqual(component.freshness_status, "CURRENT")
            self.assertFalse(component.is_cached)
            self.assertEqual(component.fetched_at, datetime(2026, 7, 13, 14, 0, tzinfo=UTC))

    def test_failed_ai_refresh_keeps_last_success_time_and_marks_cache_stale(self) -> None:
        now = datetime(2026, 7, 13, 14, 10, tzinfo=UTC)
        requested_at = "2026-07-13T22:00:00+08:00"
        with self.Session() as session:
            stock = self._stock(session)
            success = StockAIAnalysis(
                stock_id=stock.id,
                provider="openrouter",
                model="test-model",
                analysis_mode=AI_MODE_UNHELD,
                prompt_version=PROMPT_VERSION,
                analysis_date=date(2026, 7, 13),
                input_hash="successful-input-hash",
                request_payload_json=json.dumps(
                    {"analysis_context": {"analysis_requested_at": requested_at}}
                ),
                response_json="{}",
                validation_errors_json="[]",
                status="success",
                created_at=now - timedelta(minutes=10),
                updated_at=now - timedelta(minutes=9),
            )
            failed = StockAIAnalysis(
                stock_id=stock.id,
                provider="openrouter",
                model="test-model",
                analysis_mode=AI_MODE_UNHELD,
                prompt_version=PROMPT_VERSION,
                analysis_date=date(2026, 7, 13),
                input_hash="new-input-hash",
                request_payload_json="{}",
                response_json="{}",
                validation_errors_json="[]",
                status="failed",
                error_message="OpenRouter status 502",
                created_at=now - timedelta(minutes=2),
                updated_at=now - timedelta(minutes=1),
            )
            session.add_all([success, failed])
            session.commit()

            component = _ai_quality_components(stock, session, now)[0]

            self.assertEqual(component.freshness_status, "STALE")
            self.assertTrue(component.is_cached)
            self.assertEqual(component.sync_status, "failed")
            self.assertEqual(component.fetched_at, datetime(2026, 7, 13, 14, 0, tzinfo=UTC))
            self.assertEqual(component.last_error_at, now - timedelta(minutes=1))


if __name__ == "__main__":
    unittest.main()
