from __future__ import annotations

import unittest
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.database import Base, FuturesIntradayPoint, FuturesSnapshot
from backend.app.taifex_futures import (
    FuturesQuoteSnapshot,
    FuturesSession,
    _group_points_by_futures_session,
    _latest_non_closed_chart_session,
    apply_futures_chart_points,
    apply_futures_snapshot,
    current_futures_session,
    futures_session_range,
    latest_wtx_response,
    official_txf_candidate_symbols,
    parse_taifex_chart_ticks,
    parse_taifex_quote_payload,
    parse_yahoo_wtx_chart_payload,
    parse_yahoo_wtx_quote_snapshot,
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

    def test_parses_yahoo_wtx_tick_chart_payload(self) -> None:
        points = parse_yahoo_wtx_chart_payload(
            {
                "data": [
                    {
                        "chart": {
                            "timestamp": [1782370800, 1782370860, 1782416160, 1782416220],
                            "indicators": {
                                "quote": [
                                    {
                                        "close": [None, 46495, 45653, None],
                                    }
                                ]
                            },
                        }
                    }
                ]
            },
            open_price=Decimal("46430.00"),
        )

        self.assertEqual(len(points), 2)
        self.assertEqual(points[0][0].isoformat(), "2026-06-25T07:01:00+00:00")
        self.assertEqual(points[0][1], Decimal("46495.00"))
        self.assertEqual(points[0][2], Decimal("0.14"))
        self.assertEqual(points[1][0].isoformat(), "2026-06-25T19:36:00+00:00")
        self.assertEqual(points[1][1], Decimal("45653.00"))
        self.assertEqual(points[1][2], Decimal("-1.67"))

    def test_parses_yahoo_wtx_snapshot_from_chart_payload(self) -> None:
        snapshot = parse_yahoo_wtx_quote_snapshot(
            {
                "data": [
                    {
                        "chart": {
                            "meta": {
                                "regularMarketPrice": 45653,
                                "regularMarketTime": 1782416160,
                            },
                            "timestamp": [1782370800, 1782370860, 1782416160],
                            "indicators": {
                                "quote": [
                                    {
                                        "open": [None, 46430, 45653],
                                        "close": [None, 46495, 45653],
                                    }
                                ]
                            },
                        }
                    }
                ]
            }
        )

        self.assertEqual(snapshot.symbol, "WTX&")
        self.assertEqual(snapshot.current_price, Decimal("45653.00"))
        self.assertEqual(snapshot.open_price, Decimal("46430.00"))
        self.assertEqual(snapshot.price_updated_at.isoformat(), "2026-06-25T19:36:00+00:00")
        self.assertTrue(snapshot.source.startswith("Yahoo"))


class TaifexCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, future=True)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_upserts_trade_and_heartbeat_points_per_minute(self) -> None:
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

            points = session.scalars(
                select(FuturesIntradayPoint).order_by(FuturesIntradayPoint.point_time.asc())
            ).all()

        self.assertEqual(len(points), 2)
        self.assertEqual(points[0].point_time.isoformat(), "2026-06-23T13:53:00")
        self.assertEqual(points[0].price, Decimal("46575.00"))
        self.assertEqual(points[0].difference_percent, Decimal("1.93"))
        self.assertEqual(points[1].point_time.isoformat(), "2026-06-23T13:54:00")
        self.assertEqual(points[1].price, Decimal("46575.00"))
        self.assertTrue(points[1].source.endswith("heartbeat"))

    def test_skips_poll_time_heartbeat_when_quote_timestamp_is_stale(self) -> None:
        with self.Session() as session:
            apply_futures_snapshot(
                session,
                FuturesQuoteSnapshot(
                    symbol="WTX&",
                    name="台指期近一",
                    current_price=Decimal("45717.00"),
                    open_price=Decimal("46430.00"),
                    price_updated_at=datetime(2026, 6, 25, 17, 59, tzinfo=UTC),
                    source="TAIFEX MIS rtCore WTX& (TXFG6-M)",
                    source_symbol="TXFG6-M",
                ),
                now=datetime(2026, 6, 25, 18, 49, tzinfo=UTC),
            )
            session.commit()

            points = session.scalars(
                select(FuturesIntradayPoint).order_by(FuturesIntradayPoint.point_time.asc())
            ).all()

        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].point_time.isoformat(), "2026-06-25T17:59:00")
        self.assertEqual(points[0].source, "TAIFEX MIS rtCore WTX& (TXFG6-M)")
        self.assertEqual(points[0].price, Decimal("45717.00"))

    def test_closed_snapshot_does_not_create_closed_chart_points(self) -> None:
        with self.Session() as session:
            apply_futures_snapshot(
                session,
                FuturesQuoteSnapshot(
                    symbol="WTX&",
                    name="台指期近一",
                    current_price=Decimal("45805.00"),
                    open_price=Decimal("46430.00"),
                    price_updated_at=datetime(2026, 6, 26, 0, 18, tzinfo=UTC),
                    source="TAIFEX MIS rtCore WTX& (TXFG6-M)",
                    source_symbol="TXFG6-M",
                ),
                now=datetime(2026, 6, 26, 0, 18, tzinfo=UTC),
            )
            session.commit()

            points = session.scalars(select(FuturesIntradayPoint)).all()

        self.assertEqual(points, [])

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

    def test_yahoo_fallback_fills_missing_points_without_overwriting_taifex_points(self) -> None:
        with self.Session() as session:
            snapshot = FuturesQuoteSnapshot(
                symbol="WTX&",
                name="台指期近一",
                current_price=Decimal("45653.00"),
                open_price=Decimal("46430.00"),
                price_updated_at=datetime(2026, 6, 25, 19, 36, tzinfo=UTC),
                source_symbol="TXFG6-M",
            )
            futures_session = FuturesSession("night", "夜盤", datetime(2026, 6, 25, tzinfo=UTC).date())
            apply_futures_chart_points(
                session,
                snapshot,
                [
                    (datetime(2026, 6, 25, 7, 1, tzinfo=UTC), Decimal("46495.00"), Decimal("0.14")),
                ],
                futures_session=futures_session,
                source="TAIFEX MIS rtCore WTX& chart (TXFG6-M)",
                now=datetime(2026, 6, 25, 7, 2, tzinfo=UTC),
            )
            apply_futures_chart_points(
                session,
                snapshot,
                [
                    (datetime(2026, 6, 25, 7, 1, tzinfo=UTC), Decimal("46510.00"), Decimal("0.17")),
                    (datetime(2026, 6, 25, 19, 36, tzinfo=UTC), Decimal("45653.00"), Decimal("-1.67")),
                ],
                futures_session=futures_session,
                source="Yahoo FinanceChartService.ApacLibraCharts WTX&",
                overwrite_existing=False,
                now=datetime(2026, 6, 25, 19, 37, tzinfo=UTC),
            )
            session.commit()

            points = session.scalars(
                select(FuturesIntradayPoint).order_by(FuturesIntradayPoint.point_time.asc())
            ).all()

        self.assertEqual(len(points), 2)
        self.assertEqual(points[0].price, Decimal("46495.00"))
        self.assertTrue(points[0].source.startswith("TAIFEX"))
        self.assertEqual(points[1].point_time.isoformat(), "2026-06-25T19:36:00")
        self.assertEqual(points[1].price, Decimal("45653.00"))
        self.assertTrue(points[1].source.startswith("Yahoo"))

    def test_groups_yahoo_points_by_actual_futures_session(self) -> None:
        grouped = _group_points_by_futures_session(
            [
                (datetime(2026, 6, 25, 7, 1, tzinfo=UTC), Decimal("46495.00"), Decimal("0.14")),
                (datetime(2026, 6, 25, 18, 30, tzinfo=UTC), Decimal("45680.00"), Decimal("-1.62")),
                (datetime(2026, 6, 26, 0, 20, tzinfo=UTC), Decimal("45805.00"), Decimal("-1.35")),
            ]
        )

        self.assertEqual(len(grouped), 1)
        futures_session = next(iter(grouped))
        self.assertEqual(futures_session.session_type, "night")
        self.assertEqual(futures_session.session_date.isoformat(), "2026-06-25")
        self.assertEqual(len(grouped[futures_session]), 2)

    def test_latest_non_closed_chart_session_ignores_closed_points(self) -> None:
        with self.Session() as session:
            session.add_all(
                [
                    FuturesIntradayPoint(
                        symbol="WTX&",
                        session_type="night",
                        session_date=datetime(2026, 6, 25, tzinfo=UTC).date(),
                        point_time=datetime(2026, 6, 25, 20, 59, tzinfo=UTC),
                        price=Decimal("45650.00"),
                        open_price=Decimal("46430.00"),
                        difference_percent=Decimal("-1.68"),
                        source="Yahoo FinanceChartService.ApacLibraCharts WTX&",
                        fetched_at=datetime(2026, 6, 25, 21, 0, tzinfo=UTC),
                    ),
                    FuturesIntradayPoint(
                        symbol="WTX&",
                        session_type="closed",
                        session_date=datetime(2026, 6, 26, tzinfo=UTC).date(),
                        point_time=datetime(2026, 6, 26, 0, 18, tzinfo=UTC),
                        price=Decimal("45805.00"),
                        open_price=Decimal("46430.00"),
                        difference_percent=Decimal("-1.35"),
                        source="TAIFEX MIS rtCore WTX& (TXFG6-M)",
                        fetched_at=datetime(2026, 6, 26, 0, 18, tzinfo=UTC),
                    ),
                ]
            )
            session.commit()

            chart_session = _latest_non_closed_chart_session(session)

        self.assertIsNotNone(chart_session)
        self.assertEqual(chart_session.session_type, "night")
        self.assertEqual(chart_session.session_date.isoformat(), "2026-06-25")

    def test_latest_response_uses_recent_valid_chart_session_while_closed(self) -> None:
        with self.Session() as session:
            session.add(
                FuturesSnapshot(
                    symbol="WTX&",
                    name="台指期近一",
                    session_type="closed",
                    session_label="最近一盤",
                    session_date=datetime(2026, 6, 26, tzinfo=UTC).date(),
                    current_price=Decimal("45805.00"),
                    open_price=Decimal("46430.00"),
                    difference_points=Decimal("-625.00"),
                    difference_percent=Decimal("-1.35"),
                    price_updated_at=datetime(2026, 6, 26, 0, 18, tzinfo=UTC),
                    source="TAIFEX MIS rtCore WTX& (TXFG6-M)",
                    fetched_at=datetime(2026, 6, 26, 0, 18, tzinfo=UTC),
                )
            )
            session.add_all(
                [
                    FuturesIntradayPoint(
                        symbol="WTX&",
                        session_type="night",
                        session_date=datetime(2026, 6, 25, tzinfo=UTC).date(),
                        point_time=datetime(2026, 6, 25, 20, 59, tzinfo=UTC),
                        price=Decimal("45650.00"),
                        open_price=Decimal("46430.00"),
                        difference_percent=Decimal("-1.68"),
                        source="Yahoo FinanceChartService.ApacLibraCharts WTX&",
                        fetched_at=datetime(2026, 6, 25, 21, 0, tzinfo=UTC),
                    ),
                    FuturesIntradayPoint(
                        symbol="WTX&",
                        session_type="closed",
                        session_date=datetime(2026, 6, 26, tzinfo=UTC).date(),
                        point_time=datetime(2026, 6, 26, 0, 18, tzinfo=UTC),
                        price=Decimal("45805.00"),
                        open_price=Decimal("46430.00"),
                        difference_percent=Decimal("-1.35"),
                        source="TAIFEX MIS rtCore WTX& (TXFG6-M)",
                        fetched_at=datetime(2026, 6, 26, 0, 18, tzinfo=UTC),
                    ),
                ]
            )
            session.commit()

        response = latest_wtx_response(
            now=datetime(2026, 6, 26, 0, 18, tzinfo=UTC),
            session_factory=self.Session,
        )

        self.assertEqual(response["session_type"], "night")
        self.assertEqual(response["session_label"], "夜盤")
        self.assertEqual(response["session_start_at"].isoformat(), "2026-06-25T07:00:00+00:00")
        self.assertEqual(response["session_end_at"].isoformat(), "2026-06-25T21:00:00+00:00")
        self.assertEqual(len(response["chart_points"]), 1)
        self.assertEqual(response["chart_points"][0]["timestamp"].isoformat(), "2026-06-25T20:59:00+00:00")


if __name__ == "__main__":
    unittest.main()
