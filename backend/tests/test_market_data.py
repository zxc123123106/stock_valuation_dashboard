from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

from backend.app.market_data import (
    StockProfileSnapshot,
    _fetch_twse_mis_quote,
    _parse_financial_quarters,
    _parse_finmind_eps,
    _parse_monthly_revenues,
    _parse_pe_history,
    derive_pe,
    fetch_stock_quote,
    fetch_stock_pe,
    fetch_stock_profile,
)


class FinMindEpsParserTest(unittest.TestCase):
    def test_parses_quarterly_eps_rows(self) -> None:
        payload = [
            {"date": "2025-03-31", "stock_id": "4958", "type": "EPS", "value": 0.66},
            {"date": "2025-06-30", "stock_id": "4958", "type": "EPS", "value": 0.63},
            {"date": "2025-09-30", "stock_id": "4958", "type": "EPS", "value": 2.46},
            {"date": "2025-12-31", "stock_id": "4958", "type": "EPS", "value": 3.12},
            {"date": "2026-03-31", "stock_id": "4958", "type": "EPS", "value": 1.33},
        ]

        rows = _parse_finmind_eps("4958", payload)

        self.assertEqual(rows[0].eps_type, "TTM")
        self.assertEqual(rows[0].eps_value, Decimal("7.54"))
        self.assertEqual(rows[0].eps_period, "2026Q1 + 2025Q4 + 2025Q3 + 2025Q2")
        self.assertEqual(rows[1].eps_type, "LAST_YEAR")
        self.assertEqual(rows[1].eps_value, Decimal("6.87"))
        self.assertEqual(rows[1].eps_period, "2025")

    def test_requires_four_quarters(self) -> None:
        with self.assertRaisesRegex(ValueError, "four quarterly FinMind EPS rows"):
            _parse_finmind_eps(
                "4958",
                [{"date": "2026-03-31", "stock_id": "4958", "type": "EPS", "value": 1.33}],
            )


class StockProfileTest(unittest.TestCase):
    def test_fetch_profile_uses_finmind_stock_info(self) -> None:
        import backend.app.market_data as market_data

        original = market_data._fetch_finmind_stock_info
        market_data._fetch_finmind_stock_info = lambda token: [
            {
                "stock_id": "0050",
                "stock_name": "元大台灣50",
                "industry_category": "ETF",
                "type": "twse",
            }
        ]
        try:
            profile = fetch_stock_profile("0050")
        finally:
            market_data._fetch_finmind_stock_info = original

        self.assertEqual(profile.symbol, "0050")
        self.assertEqual(profile.name, "元大台灣50")
        self.assertEqual(profile.asset_type, "ETF")
        self.assertEqual(profile.market, "TWSE")


class TwseMisQuoteParserTest(unittest.TestCase):
    def test_fetch_twse_mis_quote_parses_realtime_fields(self) -> None:
        import backend.app.market_data as market_data

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self):
                return {
                    "msgArray": [
                        {
                            "z": "592.00",
                            "o": "578.00",
                            "y": "552.00",
                            "h": "607.00",
                            "l": "572.00",
                            "d": "20260616",
                            "t": "13:30:00",
                        }
                    ]
                }

        class FakeSession:
            headers: dict[str, str] = {}

            def get(self, *_args, **_kwargs):
                return FakeResponse()

        original = market_data._build_session
        market_data._build_session = lambda: FakeSession()
        try:
            quote = _fetch_twse_mis_quote("4958", "TWSE")
        finally:
            market_data._build_session = original

        self.assertEqual(quote.current_price, Decimal("592.00"))
        self.assertEqual(quote.open_price, Decimal("578.00"))
        self.assertEqual(quote.previous_close, Decimal("552.00"))
        self.assertEqual(quote.day_high, Decimal("607.00"))
        self.assertEqual(quote.day_low, Decimal("572.00"))
        self.assertEqual(quote.change_percent, Decimal("2.42"))
        self.assertEqual(quote.source, "TWSE MIS realtime quote")

    def test_missing_last_trade_uses_best_bid_instead_of_open(self) -> None:
        import backend.app.market_data as market_data

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self):
                return {
                    "msgArray": [
                        {
                            "z": "-",
                            "o": "109.45",
                            "y": "107.30",
                            "h": "111.15",
                            "l": "109.45",
                            "b": "0.0000_110.95_110.90_110.85_",
                            "a": "111.00_111.05_111.10_",
                            "d": "20260622",
                            "t": "11:45:04",
                        }
                    ]
                }

        class FakeSession:
            headers: dict[str, str] = {}

            def get(self, *_args, **_kwargs):
                return FakeResponse()

        original = market_data._build_session
        market_data._build_session = lambda: FakeSession()
        try:
            quote = _fetch_twse_mis_quote("0050", "TWSE")
        finally:
            market_data._build_session = original

        self.assertEqual(quote.current_price, Decimal("110.95"))
        self.assertEqual(quote.open_price, Decimal("109.45"))
        self.assertEqual(quote.source, "TWSE MIS best bid fallback")

    def test_missing_trade_and_order_book_does_not_use_open_as_current_price(self) -> None:
        import backend.app.market_data as market_data

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self):
                return {
                    "msgArray": [
                        {
                            "z": "-",
                            "o": "109.45",
                            "y": "107.30",
                            "b": "-",
                            "a": "-",
                        }
                    ]
                }

        class FakeSession:
            headers: dict[str, str] = {}

            def get(self, *_args, **_kwargs):
                return FakeResponse()

        original = market_data._build_session
        market_data._build_session = lambda: FakeSession()
        try:
            with self.assertRaisesRegex(ValueError, "has no current price"):
                _fetch_twse_mis_quote("0050", "TWSE")
        finally:
            market_data._build_session = original

    @patch("backend.app.market_data._fetch_finmind_latest_daily_quote")
    @patch("backend.app.market_data._fetch_twse_mis_quote", side_effect=ValueError("MIS unavailable"))
    @patch("backend.app.market_data._taiwan_market_is_open", return_value=True)
    def test_market_hours_do_not_fallback_to_daily_close(
        self,
        _market_open,
        _mis_quote,
        daily_quote,
    ) -> None:
        profile = StockProfileSnapshot(symbol="0050", name="元大台灣50", asset_type="ETF", market="TWSE")

        with self.assertRaisesRegex(ValueError, "MIS unavailable"):
            fetch_stock_quote("0050", profile=profile)

        daily_quote.assert_not_called()


class FinMindFundamentalParserTest(unittest.TestCase):
    def test_derive_pe_returns_none_for_negative_ttm_eps(self) -> None:
        current_pe = derive_pe(
            Decimal("88.00"),
            [
                type("Eps", (), {"eps_type": "TTM", "eps_value": Decimal("-2.47")})(),
            ],
        )

        self.assertIsNone(current_pe)

    def test_fetch_stock_pe_falls_back_to_finmind_per(self) -> None:
        import backend.app.market_data as market_data

        original_get_json = market_data._get_json
        original_fetch_finmind_data = market_data._fetch_finmind_data
        market_data._get_json = lambda *_args, **_kwargs: []
        market_data._fetch_finmind_data = lambda *_args, **_kwargs: [
            {"date": "2026-06-13", "stock_id": "2330", "dividend_yield": 2.1, "PER": 20.5, "PBR": 5.2},
            {"date": "2026-06-14", "stock_id": "2330", "dividend_yield": 2.2, "PER": 21.5, "PBR": 5.3},
        ]
        try:
            current_pe = fetch_stock_pe("2330")
        finally:
            market_data._get_json = original_get_json
            market_data._fetch_finmind_data = original_fetch_finmind_data

        self.assertEqual(current_pe, Decimal("21.50"))

    def test_fetch_stock_pe_treats_zero_twse_pe_as_not_applicable(self) -> None:
        import backend.app.market_data as market_data

        original_get_json = market_data._get_json
        original_fetch_finmind_data = market_data._fetch_finmind_data
        market_data._get_json = lambda *_args, **_kwargs: [{"Code": "3149", "PEratio": "0"}]
        market_data._fetch_finmind_data = lambda *_args, **_kwargs: [
            {"date": "2026-06-14", "stock_id": "3149", "dividend_yield": 0, "PER": 0, "PBR": 8.1},
        ]
        try:
            current_pe = fetch_stock_pe("3149")
        finally:
            market_data._get_json = original_get_json
            market_data._fetch_finmind_data = original_fetch_finmind_data

        self.assertIsNone(current_pe)

    def test_parse_pe_history(self) -> None:
        rows = _parse_pe_history(
            "2330",
            [
                {"date": "2026-06-13", "stock_id": "2330", "dividend_yield": 2.1, "PER": 20.5, "PBR": 5.2},
                {"date": "2026-06-14", "stock_id": "2330", "dividend_yield": 2.2, "PER": 21.5, "PBR": 5.3},
            ],
        )

        self.assertEqual(rows[-1].trade_date.isoformat(), "2026-06-14")
        self.assertEqual(rows[-1].per, Decimal("21.50"))
        self.assertEqual(rows[-1].pbr, Decimal("5.30"))
        self.assertEqual(rows[-1].dividend_yield, Decimal("2.20"))

    def test_parse_pe_history_treats_non_positive_per_as_none(self) -> None:
        rows = _parse_pe_history(
            "3149",
            [
                {"date": "2026-06-13", "stock_id": "3149", "dividend_yield": 0, "PER": 0, "PBR": 7.9},
                {"date": "2026-06-14", "stock_id": "3149", "dividend_yield": 0, "PER": -1, "PBR": 8.1},
            ],
        )

        self.assertIsNone(rows[0].per)
        self.assertIsNone(rows[1].per)

    def test_parse_monthly_revenues_calculates_mom_and_yoy(self) -> None:
        rows = _parse_monthly_revenues(
            "2330",
            [
                {"revenue_year": 2025, "revenue_month": 5, "revenue": 1000},
                {"revenue_year": 2026, "revenue_month": 4, "revenue": 1800},
                {"revenue_year": 2026, "revenue_month": 5, "revenue": 2000},
            ],
        )

        latest = rows[-1]
        self.assertEqual(latest.month_date.isoformat(), "2026-05-01")
        self.assertEqual(latest.mom_percent, Decimal("11.11"))
        self.assertEqual(latest.yoy_percent, Decimal("100.00"))

    def test_parse_financial_quarters_keeps_margin_inputs(self) -> None:
        rows = _parse_financial_quarters(
            "2330",
            [
                {"date": "2026-03-31", "type": "EPS", "value": 10.5},
                {"date": "2026-03-31", "type": "Revenue", "value": 1000},
                {"date": "2026-03-31", "type": "GrossProfit", "value": 600},
                {"date": "2026-03-31", "type": "OperatingIncome", "value": 450},
                {"date": "2026-03-31", "type": "NetIncome", "value": 400},
            ],
        )

        self.assertEqual(rows[0].eps, Decimal("10.50"))
        self.assertEqual(rows[0].revenue, Decimal("1000.00"))
        self.assertEqual(rows[0].gross_profit, Decimal("600.00"))
        self.assertEqual(rows[0].operating_income, Decimal("450.00"))
        self.assertEqual(rows[0].net_income, Decimal("400.00"))

    def test_parse_financial_quarters_accepts_finmind_taxed_income_alias(self) -> None:
        rows = _parse_financial_quarters(
            "4958",
            [
                {"date": "2026-03-31", "type": "EPS", "value": 1.33},
                {"date": "2026-03-31", "type": "Revenue", "value": 1000},
                {"date": "2026-03-31", "type": "IncomeAfterTaxes", "value": 216.4},
            ],
        )

        self.assertEqual(rows[0].net_income, Decimal("216.40"))

    def test_parse_financial_quarters_accepts_tw_financial_net_income_alias(self) -> None:
        rows = _parse_financial_quarters(
            "4958",
            [
                {"date": "2026-03-31", "type": "EPS", "value": 1.33},
                {"date": "2026-03-31", "type": "營業收入合計", "value": 1000},
                {"date": "2026-03-31", "type": "本期稅後淨利", "value": 180},
            ],
        )

        self.assertEqual(rows[0].net_income, Decimal("180.00"))


if __name__ == "__main__":
    unittest.main()
