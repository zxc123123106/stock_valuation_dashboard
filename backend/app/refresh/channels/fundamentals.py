from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from ...market_data import fetch_financial_bundle, fetch_monthly_revenues, fetch_stock_pe_snapshot
from ..models import RefreshJob


TAIPEI_TZ = ZoneInfo("Asia/Taipei")


def fetch_fundamentals_channel(job: RefreshJob, finmind_token: str | None) -> dict:
    results: dict[str, object] = {}
    errors: dict[str, Exception] = {}
    end_date = datetime.now(TAIPEI_TZ).date()

    if "CURRENT_PE" in job.categories:
        try:
            snapshot = fetch_stock_pe_snapshot(job.symbol, finmind_token=finmind_token, end_date=end_date)
            if snapshot.current_pe is None and snapshot.trade_date is None:
                raise ValueError(f"PE source returned no dated data for {job.symbol}.")
            results["CURRENT_PE"] = snapshot
        except Exception as exc:
            errors["CURRENT_PE"] = exc

    bundle_categories = {"EPS", "FINANCIAL_QUARTER"} & set(job.categories)
    if bundle_categories:
        try:
            bundle = fetch_financial_bundle(
                job.symbol,
                finmind_token=finmind_token,
                end_date=end_date,
                quarters=12,
            )
            if "EPS" in bundle_categories:
                if bundle.eps_rows:
                    results["EPS"] = bundle.eps_rows
                else:
                    errors["EPS"] = ValueError(f"FinMind returned no EPS data for {job.symbol}.")
            if "FINANCIAL_QUARTER" in bundle_categories:
                if bundle.quarters:
                    results["FINANCIAL_QUARTER"] = bundle.quarters
                else:
                    errors["FINANCIAL_QUARTER"] = ValueError(
                        f"FinMind returned no financial quarter data for {job.symbol}."
                    )
        except Exception as exc:
            for category in bundle_categories:
                errors[category] = exc

    if "MONTHLY_REVENUE" in job.categories:
        try:
            rows = fetch_monthly_revenues(
                job.symbol,
                finmind_token=finmind_token,
                end_date=end_date,
                months=36,
            )
            if not rows:
                raise ValueError(f"FinMind returned no monthly revenue data for {job.symbol}.")
            results["MONTHLY_REVENUE"] = rows
        except Exception as exc:
            errors["MONTHLY_REVENUE"] = exc

    return {"results": results, "errors": errors}
