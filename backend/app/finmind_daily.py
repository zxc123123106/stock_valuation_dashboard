from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import requests


FINMIND_API_URL = "https://api.finmindtrade.com/api/v4/data"
MONEY = Decimal("0.01")
HISTORY_CALENDAR_DAYS = 240


@dataclass(frozen=True)
class DailyPriceSnapshot:
    trade_date: date
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: int | None
    source: str
    fetched_at: datetime


def fetch_daily_prices(
    symbol: str,
    *,
    token: str | None = None,
    end_date: date | None = None,
) -> list[DailyPriceSnapshot]:
    last_date = end_date or datetime.now(UTC).date()
    start_date = last_date - timedelta(days=HISTORY_CALENDAR_DAYS)
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = requests.get(
        FINMIND_API_URL,
        headers=headers,
        params={
            "dataset": "TaiwanStockPrice",
            "data_id": symbol,
            "start_date": start_date.isoformat(),
            "end_date": last_date.isoformat(),
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != 200:
        raise ValueError(payload.get("msg") or f"FinMind returned status {payload.get('status')}.")

    fetched_at = datetime.now(UTC)
    snapshots = []
    for row in payload.get("data") or []:
        try:
            snapshots.append(
                DailyPriceSnapshot(
                    trade_date=date.fromisoformat(str(row["date"])),
                    open_price=_money(row.get("open")),
                    high_price=_money(row.get("max")),
                    low_price=_money(row.get("min")),
                    close_price=_money(row.get("close")),
                    volume=_optional_int(row.get("Trading_Volume")),
                    source="FinMind TaiwanStockPrice",
                    fetched_at=fetched_at,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid FinMind daily price row for {symbol}: {row!r}") from exc

    if not snapshots:
        raise ValueError(f"FinMind returned no daily prices for {symbol}.")

    return sorted(snapshots, key=lambda snapshot: snapshot.trade_date)


def _money(value) -> Decimal:
    try:
        return Decimal(str(value).replace(",", "").strip()).quantize(MONEY, rounding=ROUND_HALF_UP)
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"Could not parse daily price value: {value!r}") from exc


def _optional_int(value) -> int | None:
    if value in (None, "", "--", "-"):
        return None
    try:
        return int(Decimal(str(value).replace(",", "").strip()))
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"Could not parse daily volume value: {value!r}") from exc
