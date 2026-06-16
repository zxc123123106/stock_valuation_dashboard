from __future__ import annotations

import unittest
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from backend.app.database import Base, Stock, StockEPS, StockValuation, ensure_analysis_cache_columns, remove_legacy_histock_eps
import backend.app.database as database


class EpsSourceMigrationTest(unittest.TestCase):
    def test_analysis_cache_columns_are_added_to_existing_tables(self) -> None:
        old_engine = database.engine
        engine = create_engine("sqlite:///:memory:")
        try:
            database.engine = engine
            with engine.begin() as connection:
                connection.execute(text("""
                    CREATE TABLE stock_financial_quarters (
                        id INTEGER PRIMARY KEY,
                        stock_id INTEGER,
                        quarter_date DATE,
                        eps NUMERIC(12, 2)
                    )
                """))

            ensure_analysis_cache_columns()

            with engine.begin() as connection:
                columns = {
                    row._mapping["name"]
                    for row in connection.execute(text("PRAGMA table_info(stock_financial_quarters)"))
                }
        finally:
            engine.dispose()
            database.engine = old_engine

        self.assertIn("revenue", columns)
        self.assertIn("gross_profit", columns)
        self.assertIn("operating_income", columns)
        self.assertIn("net_income", columns)

    def test_removes_histock_eps_and_valuations_only(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        now = datetime.now(UTC)

        with Session(engine) as session:
            histock_stock = Stock(symbol="4958", name="4958", market="TWSE", currency="TWD")
            finmind_stock = Stock(symbol="2330", name="2330", market="TWSE", currency="TWD")
            session.add_all([histock_stock, finmind_stock])
            session.flush()

            session.add_all(
                [
                    StockEPS(
                        stock_id=histock_stock.id,
                        eps_type="TTM",
                        eps_value=Decimal("7.58"),
                        eps_period="2026Q1 + 2025Q4 + 2025Q3 + 2025Q2",
                        source="WantGoo quote + HiStock daily EPS",
                        eps_updated_at=now,
                    ),
                    StockEPS(
                        stock_id=finmind_stock.id,
                        eps_type="TTM",
                        eps_value=Decimal("74.39"),
                        eps_period="2026Q1 + 2025Q4 + 2025Q3 + 2025Q2",
                        source="FinMind financial EPS",
                        eps_updated_at=now,
                    ),
                ]
            )
            session.add_all(
                [
                    self._valuation(histock_stock.id, "WantGoo quote + HiStock daily EPS", now),
                    self._valuation(finmind_stock.id, "FinMind financial EPS", now),
                ]
            )
            session.commit()

            remove_legacy_histock_eps(session)

            eps_symbols = session.scalars(
                select(Stock.symbol).join(StockEPS, StockEPS.stock_id == Stock.id)
            ).all()
            valuation_symbols = session.scalars(
                select(Stock.symbol).join(StockValuation, StockValuation.stock_id == Stock.id)
            ).all()

        self.assertEqual(eps_symbols, ["2330"])
        self.assertEqual(valuation_symbols, ["2330"])

    @staticmethod
    def _valuation(stock_id: int, source: str, now: datetime) -> StockValuation:
        return StockValuation(
            stock_id=stock_id,
            eps_type="TTM",
            current_price=Decimal("100.00"),
            current_pe=Decimal("10.00"),
            eps_value=Decimal("10.00"),
            estimated_price=Decimal("100.00"),
            price_difference=Decimal("0.00"),
            difference_percent=Decimal("0.00"),
            valuation_status="FAIR",
            source=source,
            calculated_at=now,
        )


if __name__ == "__main__":
    unittest.main()
