from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from ..brokers import get_broker
from ..config import get_settings
from ..db.migrations import current_revision, migration_head
from ..db.models import AppSetting, CrawlerLog, Stock, StockPosition, StockRefreshState
from ..schema.data_management import UserDataDocument, UserStockExport
from .database_backup_service import list_database_backups


class ImportConflictError(RuntimeError):
    pass


class ImportValidationError(ValueError):
    pass


def _canonical_document(document: UserDataDocument) -> dict:
    stocks = sorted(document.stocks, key=lambda item: (item.display_order, item.symbol))
    seen = set()
    normalized_stocks = []
    for index, item in enumerate(stocks, start=1):
        if item.symbol in seen:
            raise ImportValidationError(f"匯入檔案包含重複標的：{item.symbol}")
        seen.add(item.symbol)
        normalized_stocks.append(
            {
                "symbol": item.symbol,
                "name": item.name.strip() or item.symbol,
                "asset_type": item.asset_type,
                "market": item.market.strip().upper() or "TWSE",
                "currency": item.currency.strip().upper() or "TWD",
                "display_order": index * 10,
                "buy_price": item.buy_price,
            }
        )
    get_broker(document.selected_broker)
    return {
        "schema_version": 1,
        "format": "stock-valuation-user-data",
        "selected_broker": document.selected_broker,
        "stocks": normalized_stocks,
    }


def _hash(value: dict) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def export_user_data(session: Session, *, include_timestamp: bool = True) -> UserDataDocument:
    stocks = list(
        session.scalars(
            select(Stock).where(Stock.is_active.is_(True)).order_by(Stock.display_order, Stock.symbol)
        ).all()
    )
    position_by_stock = {
        position.stock_id: position
        for position in session.scalars(
            select(StockPosition).where(StockPosition.stock_id.in_([stock.id for stock in stocks]))
        ).all()
    } if stocks else {}
    broker = session.get(AppSetting, "selected_broker")
    return UserDataDocument(
        exported_at=datetime.now(UTC) if include_timestamp else None,
        selected_broker=broker.value if broker else "CATHAY",
        stocks=[
            UserStockExport(
                symbol=stock.symbol,
                name=stock.name,
                asset_type=stock.asset_type,
                market=stock.market,
                currency=stock.currency,
                display_order=stock.display_order,
                buy_price=float(position_by_stock[stock.id].buy_price) if stock.id in position_by_stock else None,
            )
            for stock in stocks
        ],
    )


def user_state_revision(session: Session) -> str:
    return _hash(_canonical_document(export_user_data(session, include_timestamp=False)))


def preview_import(session: Session, document: UserDataDocument) -> dict:
    canonical = _canonical_document(document)
    normalized = UserDataDocument(**canonical)
    current_document = export_user_data(session, include_timestamp=False)
    current_by_symbol = {stock.symbol: stock for stock in current_document.stocks}
    incoming_by_symbol = {stock.symbol: stock for stock in normalized.stocks}
    current_symbols = set(current_by_symbol)
    incoming_symbols = set(incoming_by_symbol)
    position_changes = sum(
        1
        for symbol in current_symbols | incoming_symbols
        if (current_by_symbol.get(symbol).buy_price if symbol in current_by_symbol else None)
        != (incoming_by_symbol.get(symbol).buy_price if symbol in incoming_by_symbol else None)
    )
    warnings = []
    removed = sorted(current_symbols - incoming_symbols)
    if removed:
        warnings.append("未出現在匯入檔案中的標的將永久刪除，相關市場快取也會一併清除。")
    return {
        "preview_hash": _hash(canonical),
        "current_revision": user_state_revision(session),
        "normalized_document": normalized,
        "added_symbols": sorted(incoming_symbols - current_symbols),
        "retained_symbols": sorted(incoming_symbols & current_symbols),
        "removed_symbols": removed,
        "position_change_count": position_changes,
        "broker_changed": normalized.selected_broker != current_document.selected_broker,
        "warnings": warnings,
    }


def apply_import(
    session: Session,
    document: UserDataDocument,
    *,
    preview_hash: str,
    expected_revision: str,
    confirm_replace: bool,
) -> dict:
    if not confirm_replace:
        raise ImportValidationError("必須明確確認取代目前的追蹤清單與持倉設定。")
    preview = preview_import(session, document)
    if preview["preview_hash"] != preview_hash:
        raise ImportConflictError("匯入內容已在預覽後變更，請重新預覽。")
    if preview["current_revision"] != expected_revision:
        raise ImportConflictError("目前看板資料已在預覽後變更，請重新預覽。")

    normalized: UserDataDocument = preview["normalized_document"]
    incoming = {item.symbol: item for item in normalized.stocks}
    existing = {stock.symbol: stock for stock in session.scalars(select(Stock)).all()}
    now = datetime.now(UTC)

    for symbol, stock in list(existing.items()):
        if symbol not in incoming:
            session.execute(delete(StockRefreshState).where(StockRefreshState.symbol == symbol))
            session.execute(
                delete(CrawlerLog).where(
                    (CrawlerLog.job_name == f"market_refresh:{symbol}")
                    | CrawlerLog.job_name.like(f"data_refresh:{symbol}:%")
                )
            )
            session.delete(stock)

    for item in normalized.stocks:
        stock = existing.get(item.symbol)
        if stock is None:
            stock = Stock(symbol=item.symbol, name=item.name)
            session.add(stock)
            session.flush()
        stock.name = item.name
        stock.asset_type = item.asset_type
        stock.market = item.market
        stock.currency = item.currency
        stock.display_order = item.display_order
        stock.is_active = True
        stock.updated_at = now

        position = session.scalar(select(StockPosition).where(StockPosition.stock_id == stock.id))
        if item.buy_price is None:
            if position is not None:
                session.delete(position)
        elif position is None:
            session.add(StockPosition(stock_id=stock.id, buy_price=Decimal(str(item.buy_price))))
        else:
            position.buy_price = Decimal(str(item.buy_price))
            position.updated_at = now

    setting = session.get(AppSetting, "selected_broker")
    if setting is None:
        session.add(AppSetting(key="selected_broker", value=normalized.selected_broker))
    else:
        setting.value = normalized.selected_broker
        setting.updated_at = now
    session.commit()
    return preview


def database_status(session: Session, *, import_in_progress: bool = False) -> dict:
    journal_mode = str(session.execute(text("PRAGMA journal_mode")).scalar_one()).lower()
    busy_timeout = int(session.execute(text("PRAGMA busy_timeout")).scalar_one())
    foreign_keys = bool(session.execute(text("PRAGMA foreign_keys")).scalar_one())
    integrity = str(session.execute(text("PRAGMA integrity_check")).scalar_one())
    backups = list_database_backups()
    settings = get_settings()
    return {
        "journal_mode": journal_mode,
        "busy_timeout_ms": busy_timeout,
        "foreign_keys_enabled": foreign_keys,
        "integrity_status": integrity,
        "current_revision": current_revision(),
        "migration_head": migration_head(),
        "backup_retention_count": settings.database_backup_retention_count,
        "backup_hour": settings.database_backup_hour,
        "last_backup_at": backups[0]["created_at"] if backups else None,
        "backup_count": len(backups),
        "import_in_progress": import_in_progress,
    }
