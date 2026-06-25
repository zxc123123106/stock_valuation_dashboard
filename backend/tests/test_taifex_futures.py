from __future__ import annotations

import unittest
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.database import Base, FuturesIntradayPoint
from backend.app.taifex_futures import (
    FuturesQuoteSnapshot,
    FuturesSession,
    apply_futures_chart_points,
    apply_futures_snapshot,
    current_futures_session,
    futures_session_range,
    official_txf_candidate_symbols,
    parse_taifex_chart_ticks,
    parse_taifex_quote_payload,
)


class TaifexFuturesSessionTest(unittest.TestCase):
    def test_day_session(self) -> None:
        session = current_futures_session(datetime(2026, 6, 23, 1, 0, tzinfo=UTC))

        self.assertEqual(session.session_type, "day")
        self.assertEqual(session.session_label, "日盤")
        self.assertEqual(session.session_date.isoformat(), "2026-06-23")

    def test_night_session_before_midnight(self) -> None:
        session = current_futures_session(datetime(2026, 6, 23, 13, 30, tzinfo=UTC))

        self.assertEqual(session.session_type, "night")
        self.assertEqual(session.session_label, "夜盤")
        self.assertEqual(session.session_date.isoformat(), "2026-06-23")

    def test_night_session_after_midnight_uses_previous_session_date(self) -> None:
        session = current_futures_session(datetime(2026, 6, 22, 18, 30, tzinfo=UTC))

        self.assertEqual(session.session_type, "night")
        self.assertEqual(session.session_date.isoformat(), "2026-06-22")

    def test_closed_session(self) -> None:
        session = current_futures_session(datetime(2026, 6, 23, 6, 30, tzinfo=UTC))

        self.assertEqual(session.session_type, "closed")
        self.assertEqual(session.session_label, "最近一盤")
        self.assertIsNone(session.session_date)

    def test_candidate_symbols_use_near_month_and_session_suffix(self) -> None:
        night_candidates = official_txf_candidate_symbols(datetime(2026, 6, 23, 13, 30, tzinfo=UTC))
        day_candidates = official_txf_candidate_symbols(datetime(2026, 6, 24, 1, 30, tzinfo=UTC))

        self.assertEqual(night_candidates[0], "TXFG6-M")
        self.assertTrue(all(candidate.endswith("-M") for candidate in night_candidates))
        self.assertEqual(day_candidates[0], "TXFG6-F")
        self.assertTrue(all(candidate.endswith("-F") for candidate in day_candidates))

    def test_session_ranges_use_full_day_and_night_sessions(self) -> None:
        day = current_futures_session(datetime(2026, 6, 24, 1, 0, tzinfo=UTC))
        night = current_futures_session(datetime(2026, 6, 24, 13, 0, tzinfo=UTC))

        day_start, day_end = futures_session_range(day.session_type, day.session_date)
        night_start, night_end = futures_session_range(night.session_type, night.session_date)

        self.assertEqual(day_start.isoformat(), "2026-06-24T00:45:00+00:00")
        self.assertEqual(day_end.isoformat(), "2026-06-24T05:45:00+00:00")
        self.assertEqual(night_start.isoformat(), "2026-06-24T07:00:00+00:00")
        self.assertEqual(night_end.isoformat(), "2026-06-24T21:00:00+00:00")


class TaifexQuoteParserTest(unittest.TestCase):
    def test_parses_nested_rtcore_quote_values(self) -> None:
        snapshot = parse_taifex_quote_payload(
            {
                "type": "quote",
                "mode": "I020",
                "quote": {
                    "symbol": "WTX&",
                    "values": {
                        "CLastPrice": "46565",
                        "COpenPrice": "45693",
                        "CDate": "20260623",
                        "CTime": "215316",
                    },
                },
            }
        )

        self.assertEqual(snapshot.symbol, "WTX&")
        self.assertEqual(snapshot.current_price, Decimal("46565.00"))
        self.assertEqual(snapshot.open_price, Decimal("45693.00"))
        self.assertEqual(snapshot.difference_points, Decimal("872.00"))
        self.assertEqual(snapshot.difference_percent, Decimal("1.91"))

    def test_rejects_symbol_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "did not match"):
            parse_taifex_quote_payload(
                {
                    "type": "quote",
                    "quote": {
                        "symbol": "TXF",
                        "values": {"CLastPrice": "100", "COpenPrice": "99"},
                    },
                }
            )

    def test_parses_official_symbol_as_display_wtx(self) -> None:
        snapshot = parse_taifex_quote_payload(
            {
                "type": "quote",
                "quote": {
                    "symbol": "TXFG6-M",
                    "values": {
                        "55": "TXFG6-M",
                        "125": "46944.00",
                        "126": "46940.00",
                        "144": "20260623",
                        "143": "145955",
                    },
                },
            },
            expected_symbol="TXFG6-M",
            display_symbol="WTX&",
            display_name="台指期近一",
        )

        self.assertEqual(snapshot.symbol, "WTX&")
        self.assertEqual(snapshot.current_price, Decimal("46944.00"))
        self.assertEqual(snapshot.open_price, Decimal("46940.00"))
        self.assertEqual(snapshot.source_symbol, "TXFG6-M")


class TaifexChartParserTest(unittest.TestCase):
    def test_parses_day_chart_ticks_as_taipei_session_minutes(self) -> None:
        points = parse_taifex_chart_ticks(
            [["091600", "46552.00", "46572.00", "46491.00", "46561.00", "477"]],
            session_type="day",
            session_date=datetime(2026, 6, 25, tzinfo=UTC).date(),
            open_price=Decimal("46993.00"),
        )

        self.assertEqual(len(points), 1)
        self.assertEqual(points[0][0].isoformat(), "2026-06-25T01:16:00+00:00")
        self.assertEqual(points[0][1], Decimal("46561.00"))
        self.assertEqual(points[0][2], Decimal("-0.92"))

    def test_parses_after_midnight_night_chart_ticks_on_next_calendar_day(self) -> None:
        points = parse_taifex_chart_ticks(
            [["003000", "46600.00", "46600.00", "46600.00", "46600.00", "12"]],
            session_type="night",
            session_date=datetime(2026, 6, 24, tzinfo=UTC).date(),
            open_price=Decimal("46993.00"),
        )

        self.assertEqual(len(points), 1)
        self.assertEqual(points[0][0].isoformat(), "2026-06-24T16:30:00+00:00")
        self.assertEqual(points[0][1], Decimal("46600.00"))
        self.assertEqual(points[0][2], Decimal("-0.84"))


class TaifexCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def test_upserts_one_intraday_point_per_minute(self) -> None:
        with self.Session() as session:
            apply_futures_snapshot(
                session,
                FuturesQuoteSnapshot(
                    symbol="WTX&",
                    name="台指期近一",
                    current_price=Decimal("46565.00"),
                    open_price=Decimal("45693.00"),
                    price_updated_at=datetime(2026, 6, 23, 13, 53, 16, tzinfo=UTC),
                ),
                now=datetime(2026, 6, 23, 13, 54, tzinfo=UTC),
            )
            apply_futures_snapshot(
                session,
                FuturesQuoteSnapshot(
                    symbol="WTX&",
                    name="台指期近一",
                    current_price=Decimal("46575.00"),
                    open_price=Decimal("45693.00"),
                    price_updated_at=datetime(2026, 6, 23, 13, 53, 58, tzinfo=UTC),
                ),
                now=datetime(2026, 6, 23, 13, 54, tzinfo=UTC),
            )
            session.commit()

            points = session.scalars(select(FuturesIntradayPoint)).all()

        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].price, Decimal("46575.00"))
        self.assertEqual(points[0].difference_percent, Decimal("1.93"))

    def test_upserts_backfilled_chart_points_per_minute(self) -> None:
        with self.Session() as session:
            snapshot = FuturesQuoteSnapshot(
                symbol="WTX&",
                name="台指期近一",
                current_price=Decimal("46600.00"),
                open_price=Decimal("46993.00"),
                price_updated_at=datetime(2026, 6, 25, 1, 20, tzinfo=UTC),
                source_symbol="TXFG6-F",
            )
            futures_session = FuturesSession("day", "日盤", datetime(2026, 6, 25, tzinfo=UTC).date())
            apply_futures_chart_points(
                session,
                snapshot,
                [
                    (datetime(2026, 6, 25, 1, 16, tzinfo=UTC), Decimal("46561.00"), Decimal("-0.92")),
                    (datetime(2026, 6, 25, 1, 16, 30, tzinfo=UTC), Decimal("46570.00"), Decimal("-0.90")),
                    (datetime(2026, 6, 25, 1, 17, tzinfo=UTC), Decimal("46580.00"), Decimal("-0.88")),
                ],
                futures_session=futures_session,
                now=datetime(2026, 6, 25, 1, 21, tzinfo=UTC),
            )
            session.commit()

            points = session.scalars(
                select(FuturesIntradayPoint).order_by(FuturesIntradayPoint.point_time.asc())
            ).all()

        self.assertEqual(len(points), 2)
        self.assertEqual(points[0].point_time.isoformat(), "2026-06-25T01:16:00")
        self.assertEqual(points[0].price, Decimal("46570.00"))
        self.assertEqual(points[0].difference_percent, Decimal("-0.90"))
        self.assertEqual(points[1].point_time.isoformat(), "2026-06-25T01:17:00")
        self.assertEqual(points[1].price, Decimal("46580.00"))


if __name__ == "__main__":
    unittest.main()
