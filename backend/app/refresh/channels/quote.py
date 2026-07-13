from __future__ import annotations

from ...market_data import fetch_stock_profile, fetch_stock_quote
from ..models import RefreshJob


def fetch_quote_channel(job: RefreshJob, finmind_token: str | None) -> dict:
    from ..manager import _cached_profile_snapshot

    try:
        profile = None if job.profile_required else _cached_profile_snapshot(job.symbol)
        profile = profile or fetch_stock_profile(job.symbol, finmind_token=finmind_token)
        quote = fetch_stock_quote(job.symbol, profile=profile, finmind_token=finmind_token)
        return {"results": {"QUOTE": (profile, quote)}, "errors": {}}
    except Exception as exc:
        return {"results": {}, "errors": {"QUOTE": exc}}
