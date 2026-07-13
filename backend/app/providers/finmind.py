"""FinMind request and parser adapter."""

from ..finmind_daily import fetch_daily_prices
from .market_data_legacy import (
    fetch_financial_bundle,
    fetch_institutional_trading,
    fetch_monthly_revenues,
    fetch_pe_history,
    fetch_stock_eps,
    fetch_stock_profile,
    fetch_stock_quote,
)

__all__ = [name for name in globals() if name.startswith("fetch_")]
