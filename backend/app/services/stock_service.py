"""Use cases for stock ordering, positions, deletion, and cached analysis views."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from ..market_data import normalize_symbol
from ..repositories import stocks as stock_repository
from .data_quality_service import _stock_data_quality_response
from .fundamental_service import _fundamental_trends_response
from .stock_response_service import _stock_response, _valuation_response
from .technical_service import _technical_analysis_response


class StockNotFoundError(LookupError):
    pass


class StockValidationError(ValueError):
    pass


def normalize_stock_symbol(symbol: str) -> str:
    try:
        return normalize_symbol(symbol)
    except ValueError as exc:
        raise StockValidationError(str(exc)) from exc


def list_stocks(session: Session):
    return [_stock_response(stock, session) for stock in stock_repository.list_active(session)]


def get_stock(session: Session, symbol: str):
    stock = stock_repository.get_by_symbol(session, symbol)
    if stock is None:
        raise StockNotFoundError("Stock not found")
    return _stock_response(stock, session)


def reorder_stocks(session: Session, symbols: list[str]):
    normalized = [symbol.strip() for symbol in symbols]
    if len(normalized) != len(set(normalized)):
        raise StockValidationError("Stock symbols must be unique.")
    try:
        stocks = stock_repository.reorder_active(session, normalized)
    except ValueError as exc:
        raise StockValidationError(str(exc)) from exc
    return [_stock_response(stock, session) for stock in stocks]


def get_fundamental_trends(session: Session, symbol: str):
    stock = _active_stock(session, normalize_stock_symbol(symbol))
    if stock.asset_type == "ETF":
        raise StockNotFoundError("Fundamentals are not applicable to ETFs")
    return _fundamental_trends_response(stock, session)


def get_technical_analysis(session: Session, symbol: str, limit: int):
    return _technical_analysis_response(_active_stock(session, normalize_stock_symbol(symbol)), session, limit)


def get_data_quality(session: Session, symbol: str):
    return _stock_data_quality_response(_active_stock(session, normalize_stock_symbol(symbol)), session)


def set_position(session: Session, symbol: str, buy_price: float):
    normalized = normalize_stock_symbol(symbol)
    try:
        price = Decimal(str(buy_price))
    except (InvalidOperation, ValueError) as exc:
        raise StockValidationError("Buy price must be a positive number.") from exc
    if price <= 0:
        raise StockValidationError("Buy price must be a positive number.")
    stock = _active_stock(session, normalized)
    stock_repository.set_position(session, stock, price.quantize(Decimal("0.01")))
    return _stock_response(stock, session)


def clear_position(session: Session, symbol: str):
    stock = _active_stock(session, normalize_stock_symbol(symbol))
    stock_repository.clear_position(session, stock)
    return _stock_response(stock, session)


def stock_valuations(session: Session, symbol: str):
    stock = stock_repository.get_by_symbol(session, symbol)
    if stock is None:
        raise StockNotFoundError("Stock not found")
    position = stock_repository.get_position(session, stock.id)
    buy_price = position.buy_price if position else None
    return [_valuation_response(row, buy_price) for row in stock_repository.list_valuations(session, stock.id)]


def delete_stock(session: Session, symbol: str) -> str:
    normalized = normalize_stock_symbol(symbol)
    stock = stock_repository.get_by_symbol(session, normalized)
    if stock is None:
        raise StockNotFoundError("Stock not found")
    stock_repository.hard_delete(session, stock)
    return normalized


def _active_stock(session: Session, symbol: str):
    stock = stock_repository.get_by_symbol(session, symbol, active_only=True)
    if stock is None:
        raise StockNotFoundError("Stock not found")
    return stock
