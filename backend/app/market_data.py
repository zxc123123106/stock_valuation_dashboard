from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, time as clock_time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

import requests


TAIPEI_TZ = ZoneInfo("Asia/Taipei")
MONEY = Decimal("0.01")
FINMIND_DATA_URL = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TICK_URL = "https://api.finmindtrade.com/api/v4/taiwan_stock_tick_snapshot"
TWSE_OPENAPI_BASE_URL = "https://openapi.twse.com.tw/v1"
TWSE_MIS_BASE_URL = "https://mis.twse.com.tw/stock"
STOCK_INFO_CACHE_SECONDS = 6 * 60 * 60
_JSON_CACHE: dict[str, tuple[datetime, object]] = {}


@dataclass(frozen=True)
class EpsSnapshot:
    eps_type: str
    eps_value: Decimal
    eps_period: str


@dataclass(frozen=True)
class StockProfileSnapshot:
    symbol: str
    name: str
    asset_type: str
    market: str
    currency: str = "TWD"


@dataclass(frozen=True)
class QuoteSnapshot:
    symbol: str
    open_price: Decimal | None
    previous_close: Decimal | None
    day_high: Decimal | None
    day_low: Decimal | None
    current_price: Decimal
    change_percent: Decimal | None
    price_updated_at: datetime
    source: str


@dataclass(frozen=True)
class MonthlyRevenueSnapshot:
    month_date: date
    revenue: Decimal
    mom_percent: Decimal | None
    yoy_percent: Decimal | None
    source: str
    fetched_at: datetime


@dataclass(frozen=True)
class PEHistorySnapshot:
    trade_date: date
    per: Decimal | None
    pbr: Decimal | None
    dividend_yield: Decimal | None
    source: str
    fetched_at: datetime


@dataclass(frozen=True)
class FinancialQuarterSnapshot:
    quarter_date: date
    eps: Decimal
    revenue: Decimal | None
    gross_profit: Decimal | None
    operating_income: Decimal | None
    net_income: Decimal | None
    source: str
    fetched_at: datetime


@dataclass(frozen=True)
class InstitutionalTradingSnapshot:
    trade_date: date
    foreign_net: int
    investment_trust_net: int
    dealer_net: int
    total_net: int
    source: str
    fetched_at: datetime


def normalize_symbol(symbol: str) -> str:
    normalized = symbol.strip()
    if not re.fullmatch(r"\d{4,6}", normalized):
        raise ValueError("Stock symbol must be 4 to 6 digits.")
    return normalized


def fetch_stock_profile(symbol: str, *, finmind_token: str | None = None) -> StockProfileSnapshot:
    normalized_symbol = normalize_symbol(symbol)
    rows = _fetch_finmind_stock_info(finmind_token)
    row = next((item for item in rows if str(item.get("stock_id")) == normalized_symbol), None)
    if not row:
        raise ValueError(f"Could not find stock profile for {normalized_symbol} from FinMind TaiwanStockInfo.")

    market = _map_finmind_market(row.get("type"))
    industry = str(row.get("industry_category") or "").upper()
    return StockProfileSnapshot(
        symbol=normalized_symbol,
        name=str(row.get("stock_name") or normalized_symbol),
        asset_type="ETF" if industry == "ETF" else "STOCK",
        market=market,
    )


def fetch_stock_quote(
    symbol: str,
    *,
    profile: StockProfileSnapshot | None = None,
    finmind_token: str | None = None,
) -> QuoteSnapshot:
    normalized_symbol = normalize_symbol(symbol)
    profile = profile or fetch_stock_profile(normalized_symbol, finmind_token=finmind_token)
    failures: list[str] = []

    if finmind_token:
        try:
            return _fetch_finmind_realtime_quote(normalized_symbol, finmind_token)
        except Exception as exc:
            failures.append(f"FinMind realtime snapshot failed: {exc}")

    try:
        return _fetch_twse_mis_quote(normalized_symbol, profile.market)
    except Exception as exc:
        failures.append(f"TWSE MIS quote failed: {exc}")

    if _taiwan_market_is_open():
        raise ValueError("; ".join(failures) or f"Could not fetch realtime quote for {normalized_symbol}.")

    try:
        return _fetch_finmind_latest_daily_quote(normalized_symbol, finmind_token)
    except Exception as exc:
        failures.append(f"FinMind latest daily quote failed: {exc}")

    raise ValueError("; ".join(failures) or f"Could not fetch quote for {normalized_symbol}.")


def fetch_stock_pe(
    symbol: str,
    *,
    finmind_token: str | None = None,
    end_date: date | None = None,
) -> Decimal | None:
    normalized_symbol = normalize_symbol(symbol)
    session = _build_session()
    rows = _get_json(session, f"{TWSE_OPENAPI_BASE_URL}/exchangeReport/BWIBBU_ALL")
    row = next((item for item in rows if isinstance(item, dict) and item.get("Code") == normalized_symbol), None)
    twse_pe = _positive_pe(_optional_money(row.get("PEratio"))) if row else None
    if twse_pe is not None:
        return twse_pe

    history = fetch_pe_history(
        normalized_symbol,
        finmind_token=finmind_token,
        end_date=end_date,
        days=14,
    )
    latest = next((snapshot.per for snapshot in reversed(history) if snapshot.per is not None), None)
    return latest


def fetch_pe_history(
    symbol: str,
    *,
    finmind_token: str | None = None,
    end_date: date | None = None,
    days: int = 365 * 3 + 30,
) -> list[PEHistorySnapshot]:
    normalized_symbol = normalize_symbol(symbol)
    last_date = end_date or datetime.now(TAIPEI_TZ).date()
    start_date = last_date - timedelta(days=days)
    payload = _fetch_finmind_data(
        "TaiwanStockPER",
        finmind_token=finmind_token,
        params={
            "data_id": normalized_symbol,
            "start_date": start_date.isoformat(),
            "end_date": last_date.isoformat(),
        },
    )
    return _parse_pe_history(normalized_symbol, payload)


def fetch_stock_eps(
    symbol: str,
    *,
    finmind_token: str | None = None,
    end_date: date | None = None,
) -> list[EpsSnapshot]:
    normalized_symbol = normalize_symbol(symbol)
    last_date = end_date or datetime.now(TAIPEI_TZ).date()
    start_date = last_date - timedelta(days=365 * 4)
    payload = _fetch_finmind_data(
        "TaiwanStockFinancialStatements",
        finmind_token=finmind_token,
        params={
            "data_id": normalized_symbol,
            "start_date": start_date.isoformat(),
            "end_date": last_date.isoformat(),
        },
    )
    return _parse_finmind_eps(normalized_symbol, payload)


def fetch_monthly_revenues(
    symbol: str,
    *,
    finmind_token: str | None = None,
    end_date: date | None = None,
    months: int = 30,
) -> list[MonthlyRevenueSnapshot]:
    normalized_symbol = normalize_symbol(symbol)
    last_date = end_date or datetime.now(TAIPEI_TZ).date()
    start_date = last_date - timedelta(days=max(months + 14, 30) * 31)
    payload = _fetch_finmind_data(
        "TaiwanStockMonthRevenue",
        finmind_token=finmind_token,
        params={
            "data_id": normalized_symbol,
            "start_date": start_date.isoformat(),
            "end_date": last_date.isoformat(),
        },
    )
    return _parse_monthly_revenues(normalized_symbol, payload)[-months:]


def fetch_financial_quarters(
    symbol: str,
    *,
    finmind_token: str | None = None,
    end_date: date | None = None,
    quarters: int = 12,
) -> list[FinancialQuarterSnapshot]:
    normalized_symbol = normalize_symbol(symbol)
    last_date = end_date or datetime.now(TAIPEI_TZ).date()
    start_date = last_date - timedelta(days=365 * 4)
    payload = _fetch_finmind_data(
        "TaiwanStockFinancialStatements",
        finmind_token=finmind_token,
        params={
            "data_id": normalized_symbol,
            "start_date": start_date.isoformat(),
            "end_date": last_date.isoformat(),
        },
    )
    return _parse_financial_quarters(normalized_symbol, payload)[-quarters:]


def fetch_institutional_trading(
    symbol: str,
    *,
    finmind_token: str | None = None,
    end_date: date | None = None,
    days: int = 30,
) -> list[InstitutionalTradingSnapshot]:
    normalized_symbol = normalize_symbol(symbol)
    last_date = end_date or datetime.now(TAIPEI_TZ).date()
    start_date = last_date - timedelta(days=max(days * 2, 30))
    payload = _fetch_finmind_data(
        "TaiwanStockInstitutionalInvestorsBuySell",
        finmind_token=finmind_token,
        params={
            "data_id": normalized_symbol,
            "start_date": start_date.isoformat(),
            "end_date": last_date.isoformat(),
        },
    )
    return _parse_institutional_trading(normalized_symbol, payload)[-days:]


def derive_pe(current_price: Decimal, eps_rows: list[EpsSnapshot]) -> Decimal | None:
    ttm = next((row.eps_value for row in eps_rows if row.eps_type == "TTM"), None)
    if not ttm or ttm <= 0:
        return None
    return (current_price / ttm).quantize(MONEY, rounding=ROUND_HALF_UP)


def _fetch_finmind_stock_info(token: str | None) -> list[dict]:
    cache_key = f"finmind:TaiwanStockInfo:{bool(token)}"
    cached = _JSON_CACHE.get(cache_key)
    now = datetime.now(UTC)
    if cached:
        cached_at, payload = cached
        if (now - cached_at).total_seconds() < STOCK_INFO_CACHE_SECONDS:
            return payload  # type: ignore[return-value]

    rows = _fetch_finmind_data("TaiwanStockInfo", finmind_token=token)
    _JSON_CACHE[cache_key] = (now, rows)
    return rows


def _fetch_finmind_data(
    dataset: str,
    *,
    finmind_token: str | None,
    params: dict[str, str] | None = None,
) -> list[dict]:
    headers = {"Accept": "application/json"}
    if finmind_token:
        headers["Authorization"] = f"Bearer {finmind_token}"
    query = {"dataset": dataset}
    if params:
        query.update(params)

    response = requests.get(FINMIND_DATA_URL, headers=headers, params=query, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != 200:
        raise ValueError(payload.get("msg") or f"FinMind returned status {payload.get('status')}.")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError(f"FinMind {dataset} returned an invalid payload.")
    return data


def _fetch_finmind_realtime_quote(symbol: str, token: str) -> QuoteSnapshot:
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    response = requests.get(FINMIND_TICK_URL, headers=headers, params={"data_id": symbol}, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") not in (None, 200):
        raise ValueError(payload.get("msg") or f"FinMind returned status {payload.get('status')}.")

    rows = payload.get("data") or []
    if not rows:
        raise ValueError(f"FinMind returned no realtime quote for {symbol}.")
    row = rows[0]
    current_price = _money(row.get("close"))
    open_price = _optional_money(row.get("open"))
    previous_close = _previous_close_from_change(current_price, _optional_money(row.get("change_price")))
    return QuoteSnapshot(
        symbol=symbol,
        open_price=open_price,
        previous_close=previous_close,
        day_high=_optional_money(row.get("high")),
        day_low=_optional_money(row.get("low")),
        current_price=current_price,
        change_percent=_quote_change_percent(current_price, open_price),
        price_updated_at=_parse_datetime(row.get("date")),
        source="FinMind taiwan_stock_tick_snapshot",
    )


def _fetch_twse_mis_quote(symbol: str, market: str) -> QuoteSnapshot:
    session = _build_session()
    session.get(f"{TWSE_MIS_BASE_URL}/index.jsp", timeout=20)
    exchange_code = "otc" if market.upper() == "TPEX" else "tse"
    response = session.get(
        f"{TWSE_MIS_BASE_URL}/api/getStockInfo.jsp",
        params={
            "ex_ch": f"{exchange_code}_{symbol}.tw",
            "json": "1",
            "delay": "0",
            "_": str(int(time.time() * 1000)),
        },
        headers={"Referer": f"{TWSE_MIS_BASE_URL}/fibest.jsp?stock={symbol}"},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("msgArray") or []
    if not rows:
        raise ValueError(f"TWSE MIS returned no quote for {symbol}.")
    row = rows[0]
    current_price = _optional_money(row.get("z"))
    source = "TWSE MIS realtime quote"
    if current_price is None:
        current_price = _first_order_price(row.get("b"))
        source = "TWSE MIS best bid fallback"
    if current_price is None:
        current_price = _first_order_price(row.get("a"))
        source = "TWSE MIS best ask fallback"
    if current_price is None:
        raise ValueError(f"TWSE MIS quote for {symbol} has no current price.")

    open_price = _optional_money(row.get("o"))
    return QuoteSnapshot(
        symbol=symbol,
        open_price=open_price,
        previous_close=_optional_money(row.get("y")),
        day_high=_optional_money(row.get("h")),
        day_low=_optional_money(row.get("l")),
        current_price=current_price,
        change_percent=_quote_change_percent(current_price, open_price),
        price_updated_at=_parse_twse_mis_datetime(row.get("d"), row.get("t")),
        source=source,
    )


def _fetch_finmind_latest_daily_quote(symbol: str, token: str | None) -> QuoteSnapshot:
    last_date = datetime.now(TAIPEI_TZ).date()
    start_date = last_date - timedelta(days=14)
    rows = _fetch_finmind_data(
        "TaiwanStockPrice",
        finmind_token=token,
        params={
            "data_id": symbol,
            "start_date": start_date.isoformat(),
            "end_date": last_date.isoformat(),
        },
    )
    if not rows:
        raise ValueError(f"FinMind returned no daily prices for {symbol}.")

    sorted_rows = sorted(rows, key=lambda item: str(item.get("date") or ""))
    latest = sorted_rows[-1]
    previous = sorted_rows[-2] if len(sorted_rows) >= 2 else None
    current_price = _money(latest.get("close"))
    open_price = _optional_money(latest.get("open"))
    return QuoteSnapshot(
        symbol=symbol,
        open_price=open_price,
        previous_close=_optional_money(previous.get("close")) if previous else None,
        day_high=_optional_money(latest.get("max")),
        day_low=_optional_money(latest.get("min")),
        current_price=current_price,
        change_percent=_quote_change_percent(current_price, open_price),
        price_updated_at=_date_to_close_time(latest.get("date")),
        source="FinMind TaiwanStockPrice latest close",
    )


def _parse_finmind_eps(symbol: str, payload: list[dict]) -> list[EpsSnapshot]:
    quarters: dict[tuple[int, int], Decimal] = {}
    for row in payload:
        if row.get("type") != "EPS":
            continue
        row_date = date.fromisoformat(str(row.get("date")))
        quarter = (row_date.month - 1) // 3 + 1
        if quarter not in {1, 2, 3, 4}:
            continue
        eps = _optional_money(row.get("value"))
        if eps is not None:
            quarters[(row_date.year, quarter)] = eps

    if len(quarters) < 4:
        raise ValueError(f"Could not find at least four quarterly FinMind EPS rows for {symbol}.")

    latest_four = sorted(quarters.items(), key=lambda item: item[0])[-4:]
    ttm_value = sum((eps for _, eps in latest_four), Decimal("0.00")).quantize(
        MONEY,
        rounding=ROUND_HALF_UP,
    )
    ttm_period = " + ".join(_quarter_label(quarter) for quarter, _ in reversed(latest_four))
    complete_year = next(
        (
            year
            for year in sorted({year for year, _ in quarters}, reverse=True)
            if all((year, quarter) in quarters for quarter in range(1, 5))
        ),
        None,
    )
    if complete_year is None:
        raise ValueError(f"Could not find a complete fiscal year FinMind EPS set for {symbol}.")

    last_year_value = sum(
        (quarters[(complete_year, quarter)] for quarter in range(1, 5)),
        Decimal("0.00"),
    ).quantize(MONEY, rounding=ROUND_HALF_UP)

    return [
        EpsSnapshot(eps_type="TTM", eps_value=ttm_value, eps_period=ttm_period),
        EpsSnapshot(eps_type="LAST_YEAR", eps_value=last_year_value, eps_period=str(complete_year)),
    ]


def _parse_financial_quarters(symbol: str, payload: list[dict]) -> list[FinancialQuarterSnapshot]:
    rows: dict[date, dict[str, Decimal]] = {}
    for row in payload:
        try:
            quarter_date = date.fromisoformat(str(row.get("date")))
        except ValueError as exc:
            raise ValueError(f"Invalid FinMind financial date for {symbol}: {row!r}") from exc
        value = _optional_money(row.get("value"))
        if value is None:
            continue
        metric_key = _financial_metric_key(row.get("type"))
        if metric_key:
            rows.setdefault(quarter_date, {})[metric_key] = value

    if not any("eps" in values for values in rows.values()):
        raise ValueError(f"FinMind returned no quarterly EPS rows for {symbol}.")

    fetched_at = datetime.now(UTC)
    return [
        FinancialQuarterSnapshot(
            quarter_date=quarter_date,
            eps=values["eps"],
            revenue=values.get("revenue"),
            gross_profit=values.get("gross_profit"),
            operating_income=values.get("operating_income"),
            net_income=values.get("net_income"),
            source="FinMind TaiwanStockFinancialStatements",
            fetched_at=fetched_at,
        )
        for quarter_date, values in sorted(rows.items())
        if "eps" in values
    ]


def _parse_monthly_revenues(symbol: str, payload: list[dict]) -> list[MonthlyRevenueSnapshot]:
    revenue_by_month: dict[date, Decimal] = {}
    fetched_at = datetime.now(UTC)
    for row in payload:
        try:
            year = int(row.get("revenue_year") or row.get("year"))
            month = int(row.get("revenue_month") or row.get("month"))
            month_date = date(year, month, 1)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid FinMind monthly revenue row for {symbol}: {row!r}") from exc

        revenue = _optional_decimal(row.get("revenue"))
        if revenue is None:
            revenue = _optional_decimal(row.get("revenue_thousand"))
        if revenue is None:
            raise ValueError(f"Missing FinMind monthly revenue value for {symbol}: {row!r}")
        revenue_by_month[month_date] = revenue.quantize(MONEY, rounding=ROUND_HALF_UP)

    if not revenue_by_month:
        raise ValueError(f"FinMind returned no monthly revenues for {symbol}.")

    snapshots: list[MonthlyRevenueSnapshot] = []
    for month_date, revenue in sorted(revenue_by_month.items()):
        previous_month = _previous_month(month_date)
        previous_year = date(month_date.year - 1, month_date.month, 1)
        mom_percent = _decimal_percent(revenue - revenue_by_month[previous_month], revenue_by_month[previous_month]) if previous_month in revenue_by_month else None
        yoy_percent = _decimal_percent(revenue - revenue_by_month[previous_year], revenue_by_month[previous_year]) if previous_year in revenue_by_month else None
        snapshots.append(
            MonthlyRevenueSnapshot(
                month_date=month_date,
                revenue=revenue,
                mom_percent=mom_percent,
                yoy_percent=yoy_percent,
                source="FinMind TaiwanStockMonthRevenue",
                fetched_at=fetched_at,
            )
        )

    return snapshots


def _parse_pe_history(symbol: str, payload: list[dict]) -> list[PEHistorySnapshot]:
    snapshots: list[PEHistorySnapshot] = []
    fetched_at = datetime.now(UTC)
    for row in payload:
        try:
            trade_date = date.fromisoformat(str(row.get("date")))
        except ValueError as exc:
            raise ValueError(f"Invalid FinMind PER date for {symbol}: {row!r}") from exc

        snapshots.append(
            PEHistorySnapshot(
                trade_date=trade_date,
                per=_positive_pe(_optional_money(row.get("PER"))),
                pbr=_optional_money(row.get("PBR")),
                dividend_yield=_optional_money(row.get("dividend_yield")),
                source="FinMind TaiwanStockPER",
                fetched_at=fetched_at,
            )
        )

    if not snapshots:
        raise ValueError(f"FinMind returned no PE history rows for {symbol}.")
    return sorted(snapshots, key=lambda snapshot: snapshot.trade_date)


def _parse_institutional_trading(symbol: str, payload: list[dict]) -> list[InstitutionalTradingSnapshot]:
    daily: dict[date, dict[str, int]] = {}
    for row in payload:
        try:
            trade_date = date.fromisoformat(str(row.get("date")))
        except ValueError as exc:
            raise ValueError(f"Invalid FinMind institutional date for {symbol}: {row!r}") from exc

        name = str(row.get("name") or "")
        buy = _optional_int(row.get("buy")) or 0
        sell = _optional_int(row.get("sell")) or 0
        net = buy - sell
        bucket = daily.setdefault(trade_date, {"foreign": 0, "trust": 0, "dealer": 0})
        normalized_name = name.lower()
        if "foreign" in normalized_name:
            bucket["foreign"] += net
        elif "investment_trust" in normalized_name:
            bucket["trust"] += net
        elif "dealer" in normalized_name:
            bucket["dealer"] += net

    if not daily:
        raise ValueError(f"FinMind returned no institutional trading rows for {symbol}.")

    fetched_at = datetime.now(UTC)
    snapshots = []
    for trade_date, values in sorted(daily.items()):
        total = values["foreign"] + values["trust"] + values["dealer"]
        snapshots.append(
            InstitutionalTradingSnapshot(
                trade_date=trade_date,
                foreign_net=values["foreign"],
                investment_trust_net=values["trust"],
                dealer_net=values["dealer"],
                total_net=total,
                source="FinMind TaiwanStockInstitutionalInvestorsBuySell",
                fetched_at=fetched_at,
            )
        )
    return snapshots


def _build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 StockValuationDashboard/0.1",
            "Accept": "application/json, text/plain, */*",
        }
    )
    return session


def _get_json(session: requests.Session, url: str, params: dict[str, str] | None = None):
    response = session.get(url, params=params, timeout=20)
    response.raise_for_status()
    return response.json()


def _money(value) -> Decimal:
    parsed = _optional_money(value)
    if parsed is None:
        raise ValueError(f"Could not parse numeric value: {value!r}")
    return parsed


def _optional_money(value) -> Decimal | None:
    parsed = _optional_decimal(value)
    if parsed is None:
        return None
    return parsed.quantize(MONEY, rounding=ROUND_HALF_UP)


def _positive_pe(value: Decimal | None) -> Decimal | None:
    if value is None or value <= 0:
        return None
    return value


def _optional_decimal(value) -> Decimal | None:
    if value in (None, "", "--", "-", "N/A"):
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        return None


def _optional_int(value) -> int | None:
    parsed = _optional_decimal(value)
    return int(parsed) if parsed is not None else None


def _decimal_percent(numerator: Decimal, denominator: Decimal | None) -> Decimal | None:
    if denominator is None or denominator == 0:
        return None
    return (numerator / denominator * Decimal("100")).quantize(MONEY, rounding=ROUND_HALF_UP)


def _previous_month(value: date) -> date:
    if value.month == 1:
        return date(value.year - 1, 12, 1)
    return date(value.year, value.month - 1, 1)


def _financial_metric_key(value) -> str | None:
    normalized = str(value or "").strip().lower().replace(" ", "").replace("_", "")
    aliases = {
        "eps": "eps",
        "earningspershare": "eps",
        "每股盈餘": "eps",
        "revenue": "revenue",
        "operatingrevenue": "revenue",
        "營業收入": "revenue",
        "營業收入合計": "revenue",
        "grossprofit": "gross_profit",
        "營業毛利": "gross_profit",
        "營業毛利（毛損）": "gross_profit",
        "operatingincome": "operating_income",
        "operatingprofit": "operating_income",
        "營業利益": "operating_income",
        "營業利益（損失）": "operating_income",
        "incomeaftertaxes": "net_income",
        "incomefromcontinuingoperationsaftertax": "net_income",
        "profitloss": "net_income",
        "netincome": "net_income",
        "profitaftertax": "net_income",
        "本期稅後淨利": "net_income",
        "本期稅後淨利淨損": "net_income",
        "本期淨利": "net_income",
        "本期淨利（淨損）": "net_income",
        "本期淨利淨損": "net_income",
        "稅後淨利": "net_income",
        "稅後淨利（淨損）": "net_income",
        "稅後淨利淨損": "net_income",
        "本期稅後淨利（淨損）": "net_income",
    }
    return aliases.get(normalized)


def _quote_change_percent(current_price: Decimal, open_price: Decimal | None) -> Decimal | None:
    if open_price is None or open_price == 0:
        return None
    return ((current_price - open_price) / open_price * Decimal("100")).quantize(MONEY, rounding=ROUND_HALF_UP)


def _first_order_price(value) -> Decimal | None:
    for item in str(value or "").split("_"):
        price = _optional_money(item)
        if price is not None:
            return price
    return None


def _taiwan_market_is_open(now: datetime | None = None) -> bool:
    local_now = now or datetime.now(TAIPEI_TZ)
    local_time = local_now.astimezone(TAIPEI_TZ).time().replace(tzinfo=None)
    return local_now.weekday() < 5 and clock_time(9, 0) <= local_time < clock_time(14, 0)


def _previous_close_from_change(current_price: Decimal, change_price: Decimal | None) -> Decimal | None:
    if change_price is None:
        return None
    return (current_price - change_price).quantize(MONEY, rounding=ROUND_HALF_UP)


def _parse_datetime(value) -> datetime:
    if not value:
        return datetime.now(TAIPEI_TZ)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TAIPEI_TZ)
    return parsed.astimezone(TAIPEI_TZ)


def _parse_twse_mis_datetime(date_value, time_value) -> datetime:
    try:
        raw_date = str(date_value)
        raw_time = str(time_value or "00:00:00")
        parsed = datetime.strptime(f"{raw_date} {raw_time}", "%Y%m%d %H:%M:%S")
        return parsed.replace(tzinfo=TAIPEI_TZ)
    except (TypeError, ValueError):
        return datetime.now(TAIPEI_TZ)


def _date_to_close_time(value) -> datetime:
    trade_date = date.fromisoformat(str(value))
    return datetime.combine(trade_date, datetime.min.time().replace(hour=14), tzinfo=TAIPEI_TZ)


def _quarter_label(quarter: tuple[int, int]) -> str:
    return f"{quarter[0]}Q{quarter[1]}"


def _map_finmind_market(value) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"tpex", "otc"}:
        return "TPEX"
    return "TWSE"
