from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from ...finmind_daily import fetch_daily_prices
from ...market_data import fetch_pe_history
from ..models import RefreshJob


TAIPEI_TZ = ZoneInfo("Asia/Taipei")


def fetch_history_channel(job: RefreshJob, finmind_token: str | None) -> dict:
    results: dict[str, object] = {}
    errors: dict[str, Exception] = {}
    end_date = datetime.now(TAIPEI_TZ).date()
    if "TECHNICAL_DAILY" in job.categories:
        try:
            rows = fetch_daily_prices(job.symbol, token=finmind_token, end_date=end_date)
            if not rows:
                raise ValueError(f"FinMind returned no daily prices for {job.symbol}.")
            results["TECHNICAL_DAILY"] = rows
        except Exception as exc:
            errors["TECHNICAL_DAILY"] = exc
    if "PE_HISTORY" in job.categories:
        try:
            rows = fetch_pe_history(job.symbol, finmind_token=finmind_token, end_date=end_date)
            if not rows:
                raise ValueError(f"FinMind returned no PE history for {job.symbol}.")
            results["PE_HISTORY"] = rows
        except Exception as exc:
            errors["PE_HISTORY"] = exc
    return {"results": results, "errors": errors}
