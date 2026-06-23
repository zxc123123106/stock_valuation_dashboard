from __future__ import annotations

import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from backend.app.database import Base, Stock, StockEPS, StockMetric, StockPEHistory, StockValuation, apply_layered_stock_refresh, backfill_latest_metric_pe_from_history, ensure_analysis_cache_columns, remove_legacy_histock_eps
import backend.app.database as database


class EpsSourceMigrationTest(unittest.TestCase):
    def test_latest_finmind_pe_history_backfills_stale_current_pe(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        now = datetime.now(UTC)

        with Session(engine) as session:
            stock = Stock(symbol="4958", name="臻鼎-KY", asset_type="STOCK", market="TWSE")
            session.add(stock)
            session.flush()
            session.add(StockMetric(
                stock_id=stock.id,
                current_price=Decimal("631.00"),
                current_pe=Decimal("90.55"),
                price_updated_at=now,
                pe_updated_at=now,
                source="TWSE OpenAPI BWIBBU_ALL",
            ))
            session.add(StockPEHistory(
                stock_id=stock.id,
                trade_date=date(2026, 6, 22),
                per=Decimal("87.73"),
                source="FinMind TaiwanStockPER",
                fetched_at=now,
            ))
            session.flush()

            backfill_latest_metric_pe_from_history(session)
            session.flush()
            metric = session.scalar(select(StockMetric).where(StockMetric.stock_id == stock.id))

        engine.dispose()

        self.assertEqual(metric.current_pe, Decimal("87.73"))
        self.assertEqual(metric.pe_data_date, date(2026, 6, 22))

    def test_negative_eps_with_no_valid_pe_does_not_create_valuations(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        now = datetime.now(UTC)

        profile = SimpleNamespace(
            symbol="3149",
            name="正達",
            asset_type="STOCK",
            market="TWSE",
            currency="TWD",
        )
        quote = SimpleNamespace(
            open_price=Decimal("90.00"),
            previous_close=Decimal("87.40"),
            day_high=Decimal("90.00"),
            day_low=Decimal("87.70"),
            current_price=Decimal("88.00"),
            change_percent=Decimal("-2.22"),
            price_updated_at=now,
        )
        eps_rows = [
            SimpleNamespace(eps_type="TTM", eps_value=Decimal("-2.47"), eps_period="2026Q1 + 2025Q4 + 2025Q3 + 2025Q2"),
            SimpleNamespace(eps_type="LAST_YEAR", eps_value=Decimal("-2.88"), eps_period="2025"),
        ]

        with Session(engine) as session:
            apply_layered_stock_refresh(
                session,
                profile=profile,
                quote=quote,
                current_pe=None,
                pe_updated_at=None,
                eps_rows=eps_rows,
                eps_updated_at=now,
                source="test",
                calculated_at=now,
            )
            session.commit()

            valuation_count = session.scalar(select(func.count()).select_from(StockValuation))
            eps_count = session.scalar(select(func.count()).select_from(StockEPS))

        engine.dispose()

        self.assertEqual(eps_count, 2)
        self.assertEqual(valuation_count, 0)

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
