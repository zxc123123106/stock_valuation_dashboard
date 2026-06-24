from __future__ import annotations

import unittest
from datetime import UTC, date, datetime
from decimal import Decimal

from backend.app.database import StockFinancialQuarter, StockMonthlyRevenue
from backend.app.main import _fundamental_trend_categories


FETCHED_AT = datetime(2026, 6, 24, 8, 0, tzinfo=UTC)


def quarter(
    quarter_date: date,
    eps: str,
    revenue: str,
    gross_profit: str,
    operating_income: str,
    net_income: str,
) -> StockFinancialQuarter:
    return StockFinancialQuarter(
        stock_id=1,
        quarter_date=quarter_date,
        eps=Decimal(eps),
        revenue=Decimal(revenue),
        gross_profit=Decimal(gross_profit),
        operating_income=Decimal(operating_income),
        net_income=Decimal(net_income),
        source="FinMind TaiwanStockFinancialStatements",
        fetched_at=FETCHED_AT,
    )


def revenue(month_date: date, value: str, mom: str | None = None, yoy: str | None = None) -> StockMonthlyRevenue:
    return StockMonthlyRevenue(
        stock_id=1,
        month_date=month_date,
        revenue=Decimal(value),
        mom_percent=Decimal(mom) if mom is not None else None,
        yoy_percent=Decimal(yoy) if yoy is not None else None,
        source="FinMind TaiwanStockMonthRevenue",
        fetched_at=FETCHED_AT,
    )


class FundamentalTrendCategoryTest(unittest.TestCase):
    def test_quarterly_trends_include_same_quarter_previous_year(self) -> None:
        quarters = [
            quarter(date(2024, 6, 30), "0.60", "100", "20", "10", "8"),
            quarter(date(2024, 9, 30), "0.70", "110", "22", "12", "9"),
            quarter(date(2024, 12, 31), "0.80", "120", "24", "14", "10"),
            quarter(date(2025, 3, 31), "0.66", "100", "20", "10", "8"),
            quarter(date(2025, 6, 30), "0.63", "110", "22", "11", "9"),
            quarter(date(2025, 9, 30), "2.46", "120", "30", "15", "12"),
            quarter(date(2025, 12, 31), "3.12", "140", "35", "20", "16"),
            quarter(date(2026, 3, 31), "1.33", "150", "36", "18", "15"),
        ]

        categories = {category.key: category for category in _fundamental_trend_categories(quarters, [])}

        eps = categories["eps"]
        self.assertEqual([point.period for point in eps.points], ["2025Q1", "2025Q2", "2025Q3", "2025Q4", "2026Q1"])
        self.assertEqual(eps.summary[0].value, 1.33)
        self.assertEqual(eps.summary[1].value, 101.52)
        self.assertEqual(eps.summary[2].value, 173.19)
        self.assertEqual(eps.points[-1].yoy_percent, 101.52)

        gross_margin = categories["gross_margin"]
        self.assertEqual(gross_margin.summary[0].value, 24.0)
        self.assertEqual(gross_margin.summary[1].value, -1.0)
        self.assertEqual([point.period for point in gross_margin.points], ["2025Q1", "2025Q2", "2025Q3", "2025Q4", "2026Q1"])

    def test_monthly_revenue_trends_include_same_month_previous_year(self) -> None:
        revenues = [
            revenue(date(2025, month, 1), str(1000 + month), "1.00", "10.00")
            for month in range(1, 13)
        ] + [
            revenue(date(2026, month, 1), str(2000 + month), "2.00", "20.00")
            for month in range(1, 6)
        ]
        revenues[-1].mom_percent = Decimal("6.61")
        revenues[-1].yoy_percent = Decimal("37.40")
        revenues[-2].yoy_percent = Decimal("15.00")
        revenues[-3].yoy_percent = Decimal("4.00")

        categories = {category.key: category for category in _fundamental_trend_categories([], revenues)}

        monthly = categories["monthly_revenue"]
        self.assertEqual(monthly.points[0].period, "2025/05")
        self.assertEqual(monthly.points[-1].period, "2026/05")
        self.assertEqual(len(monthly.points), 13)
        self.assertEqual(monthly.summary[0].value, 37.4)
        self.assertEqual(monthly.summary[1].value, 6.61)
        self.assertEqual(monthly.summary[2].value, 18.8)


if __name__ == "__main__":
    unittest.main()
