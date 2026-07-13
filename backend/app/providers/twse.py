"""TWSE request adapter.

The legacy parser remains the source of truth during this compatibility cycle;
this module gives services a provider-only import boundary.
"""

from .market_data_legacy import (
    _fetch_twse_mis_profile as fetch_profile,
    _fetch_twse_mis_quote as fetch_quote,
    fetch_stock_pe_snapshot as fetch_current_pe,
)

__all__ = ["fetch_profile", "fetch_quote", "fetch_current_pe"]
