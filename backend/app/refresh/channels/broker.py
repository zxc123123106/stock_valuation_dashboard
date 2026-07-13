from __future__ import annotations

from ...yahoo_broker import fetch_broker_trading
from ..models import RefreshJob


def fetch_broker_channel(job: RefreshJob) -> dict:
    try:
        return {"results": {"BROKER_TRADING": fetch_broker_trading(job.symbol)}, "errors": {}}
    except Exception as exc:
        return {"results": {}, "errors": {"BROKER_TRADING": exc}}
