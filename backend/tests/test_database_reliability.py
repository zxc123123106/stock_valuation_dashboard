from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from backend.app.db.models import (
    AppSetting,
    Base,
    Stock,
    StockInstitutionalTrading,
    StockPosition,
)
from backend.app.schema.data_management import UserDataDocument, UserStockExport
from backend.app.services import data_management_service
from backend.app.services import database_backup_service


class DatabaseCascadeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", future=True)

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_stock_delete_cascades_position_and_institutional_rows(self) -> None:
        with Session(self.engine) as session:
            stock = Stock(symbol="4958", name="臻鼎-KY")
            session.add(stock)
            session.flush()
            session.add(StockPosition(stock_id=stock.id, buy_price=500))
            session.add(
                StockInstitutionalTrading(
                    stock_id=stock.id,
                    trade_date=date(2026, 7, 14),
                    foreign_net=1,
                    investment_trust_net=2,
                    dealer_net=3,
                    total_net=6,
                    source="test",
                    fetched_at=datetime(2026, 7, 14, 2, 0, tzinfo=UTC),
                )
            )
            session.commit()
            session.delete(stock)
            session.commit()
            self.assertEqual(session.scalar(select(func.count()).select_from(StockPosition)), 0)
            self.assertEqual(session.scalar(select(func.count()).select_from(StockInstitutionalTrading)), 0)


class DatabaseBackupTest(unittest.TestCase):
    def test_online_backup_is_valid_and_retention_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite3"
            backup_dir = root / "backups"
            backup_dir.mkdir()
            with closing(sqlite3.connect(source)) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
                connection.execute("INSERT INTO sample VALUES ('preserved')")
                connection.commit()

            settings = SimpleNamespace(database_backup_retention_count=2, database_backup_hour=3)
            with (
                patch.object(database_backup_service, "database_file_path", return_value=source),
                patch.object(database_backup_service, "backup_directory", return_value=backup_dir),
                patch.object(database_backup_service, "get_settings", return_value=settings),
            ):
                first = database_backup_service.create_database_backup("manual")
                database_backup_service.create_database_backup("manual")
                database_backup_service.create_database_backup("manual")
                backups = database_backup_service.list_database_backups()

            self.assertEqual(len(backups), 2)
            self.assertNotIn(first["filename"], {backup["filename"] for backup in backups})
            with closing(sqlite3.connect(backup_dir / backups[0]["filename"])) as connection:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("SELECT value FROM sample").fetchone()[0], "preserved")


class UserDataImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", future=True)

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_preview_then_replace_preserves_retained_stock_and_replaces_user_state(self) -> None:
        with Session(self.engine) as session:
            kept = Stock(symbol="4958", name="臻鼎-KY", display_order=10)
            removed = Stock(symbol="0050", name="元大台灣50", asset_type="ETF", display_order=20)
            session.add_all([kept, removed])
            session.flush()
            session.add(StockPosition(stock_id=kept.id, buy_price=490))
            session.add(AppSetting(key="selected_broker", value="CATHAY"))
            session.commit()
            kept_id = kept.id

            document = UserDataDocument(
                selected_broker="CATHAY",
                stocks=[
                    UserStockExport(
                        symbol="4958",
                        name="臻鼎-KY",
                        display_order=20,
                        buy_price=510,
                    ),
                    UserStockExport(
                        symbol="2301",
                        name="光寶科",
                        display_order=10,
                    ),
                ],
            )
            preview = data_management_service.preview_import(session, document)
            result = data_management_service.apply_import(
                session,
                document,
                preview_hash=preview["preview_hash"],
                expected_revision=preview["current_revision"],
                confirm_replace=True,
            )

            stocks = {stock.symbol: stock for stock in session.scalars(select(Stock)).all()}
            position = session.scalar(select(StockPosition).where(StockPosition.stock_id == kept_id))
            self.assertEqual(result["added_symbols"], ["2301"])
            self.assertEqual(result["removed_symbols"], ["0050"])
            self.assertEqual(set(stocks), {"2301", "4958"})
            self.assertEqual(stocks["4958"].id, kept_id)
            self.assertEqual(float(position.buy_price), 510.0)

    def test_stale_preview_revision_is_rejected(self) -> None:
        with Session(self.engine) as session:
            session.add(Stock(symbol="2330", name="台積電", display_order=10))
            session.commit()
            document = data_management_service.export_user_data(session)
            preview = data_management_service.preview_import(session, document)
            session.add(Stock(symbol="2301", name="光寶科", display_order=20))
            session.commit()
            with self.assertRaises(data_management_service.ImportConflictError):
                data_management_service.apply_import(
                    session,
                    document,
                    preview_hash=preview["preview_hash"],
                    expected_revision=preview["current_revision"],
                    confirm_replace=True,
                )


if __name__ == "__main__":
    unittest.main()
