"""Backward-compatible market-data facade."""

from .providers.market_data_legacy import *

__all__ = [name for name in globals() if not name.startswith("__")]
