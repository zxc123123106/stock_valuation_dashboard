"""Stock aggregate persistence operations."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..db.models import CrawlerLog, Stock, StockPosition, StockRefreshState, StockValuation


def list_active(session: Session) -> list[Stock]:
    return list(
        session.scalars(
            select(Stock)
            .where(Stock.is_active.is_(True))
            .order_by(Stock.display_order, Stock.symbol)
        ).all()
    )


def get_by_symbol(session: Session, symbol: str, *, active_only: bool = False) -> Stock | None:
    query = select(Stock).where(Stock.symbol == symbol)
    if active_only:
        query = query.where(Stock.is_active.is_(True))
    return session.scalar(query)


def reorder_active(session: Session, symbols: list[str]) -> list[Stock]:
    stocks = list_active(session)
    by_symbol = {stock.symbol: stock for stock in stocks}
    if set(symbols) != set(by_symbol):
        raise ValueError("Symbols must match all active stocks.")
    now = datetime.now(UTC)
    for index, symbol in enumerate(symbols, start=1):
        by_symbol[symbol].display_order = index * 10
        by_symbol[symbol].updated_at = now
    session.commit()
    return list_active(session)


def set_position(session: Session, stock: Stock, buy_price: Decimal) -> None:
    now = datetime.now(UTC)
    position = session.scalar(select(StockPosition).where(StockPosition.stock_id == stock.id))
    if position is None:
        session.add(StockPosition(stock_id=stock.id, buy_price=buy_price))
    else:
        position.buy_price = buy_price
        position.updated_at = now
    stock.updated_at = now
    session.commit()


def clear_position(session: Session, stock: Stock) -> None:
    position = session.scalar(select(StockPosition).where(StockPosition.stock_id == stock.id))
    if position is None:
        return
    session.delete(position)
    stock.updated_at = datetime.now(UTC)
    session.commit()


def list_valuations(session: Session, stock_id: int) -> list[StockValuation]:
    return list(
        session.scalars(
            select(StockValuation)
            .where(StockValuation.stock_id == stock_id)
            .order_by(StockValuation.eps_type.desc())
        ).all()
    )


def get_position(session: Session, stock_id: int) -> StockPosition | None:
    return session.scalar(select(StockPosition).where(StockPosition.stock_id == stock_id))


def hard_delete(session: Session, stock: Stock) -> None:
    session.execute(delete(StockRefreshState).where(StockRefreshState.symbol == stock.symbol))
    session.execute(
        delete(CrawlerLog).where(
            (CrawlerLog.job_name == f"market_refresh:{stock.symbol}")
            | CrawlerLog.job_name.like(f"data_refresh:{stock.symbol}:%")
        )
    )
    session.delete(stock)
    session.commit()
