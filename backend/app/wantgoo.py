from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

import requests


TAIPEI_TZ = ZoneInfo("Asia/Taipei")
MONEY = Decimal("0.01")
TWSE_OPENAPI_BASE_URL = "https://openapi.twse.com.tw/v1"
WANTGOO_PROFILE_CACHE_SECONDS = 6 * 60 * 60
WANTGOO_QUOTE_CACHE_SECONDS = 55
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


@dataclass(frozen=True)
class StockSnapshot:
    symbol: str
    name: str
    market: str
    currency: str
    open_price: Decimal | None
    previous_close: Decimal | None
    day_high: Decimal | None
    day_low: Decimal | None
    current_price: Decimal
    change_percent: Decimal | None
    current_pe: Decimal
    price_updated_at: datetime
    eps_rows: list[EpsSnapshot]
    source: str
    fetched_at: datetime


def fetch_stock_snapshot(symbol: str, base_url: str) -> StockSnapshot:
    normalized_symbol = normalize_symbol(symbol)
    session = _build_session()

    profile = _profile_snapshot(normalized_symbol, _fetch_wantgoo_profile(session, normalized_symbol, base_url))
    quote = _quote_snapshot(normalized_symbol, _fetch_wantgoo_quote(session, normalized_symbol, base_url))
    from .histock_eps import fetch_stock_eps as fetch_histock_eps

    eps_rows = fetch_histock_eps(normalized_symbol)
    current_pe = _fetch_twse_pe(session, normalized_symbol) or derive_pe(quote.current_price, eps_rows)

    return StockSnapshot(
        symbol=normalized_symbol,
        name=profile.name,
        market=profile.market,
        currency=profile.currency,
        open_price=quote.open_price,
        previous_close=quote.previous_close,
        day_high=quote.day_high,
        day_low=quote.day_low,
        current_price=quote.current_price,
        change_percent=quote.change_percent,
        current_pe=current_pe,
        price_updated_at=quote.price_updated_at,
        eps_rows=eps_rows,
        source="WantGoo quote + TWSE PE + HiStock EPS",
        fetched_at=datetime.now(TAIPEI_TZ),
    )


def fetch_stock_profile(symbol: str, base_url: str) -> StockProfileSnapshot:
    normalized_symbol = normalize_symbol(symbol)
    session = _build_session()
    return _profile_snapshot(normalized_symbol, _fetch_wantgoo_profile(session, normalized_symbol, base_url))


def fetch_stock_quote(symbol: str, base_url: str) -> QuoteSnapshot:
    normalized_symbol = normalize_symbol(symbol)
    session = _build_session()
    return _quote_snapshot(normalized_symbol, _fetch_wantgoo_quote(session, normalized_symbol, base_url))


def fetch_stock_pe(symbol: str) -> Decimal | None:
    normalized_symbol = normalize_symbol(symbol)
    session = _build_session()
    return _fetch_twse_pe(session, normalized_symbol)


def fetch_stock_eps(symbol: str) -> list[EpsSnapshot]:
    normalized_symbol = normalize_symbol(symbol)
    from .histock_eps import fetch_stock_eps as fetch_histock_eps

    return fetch_histock_eps(normalized_symbol)


def normalize_symbol(symbol: str) -> str:
    normalized = symbol.strip()
    if not re.fullmatch(r"\d{4,6}", normalized):
        raise ValueError("Stock symbol must be 4 to 6 digits.")
    return normalized


def _build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 StockValuationDashboard/0.1",
            "Accept": "application/json, text/plain, */*",
        }
    )
    session.cookies.set("client_fingerprint", "stock-valuation-dashboard")
    session.cookies.set("BID", "stock-valuation-dashboard")
    return session


def _fetch_wantgoo_profile(session: requests.Session, symbol: str, base_url: str) -> dict:
    rows = _get_cached_json(
        session,
        f"{base_url.rstrip('/')}/investrue/all-alive",
        ttl_seconds=WANTGOO_PROFILE_CACHE_SECONDS,
    )
    profile = next(
        (
            row
            for row in rows
            if isinstance(row, dict)
            and row.get("id") == symbol
            and str(row.get("type") or "").upper() in {"STOCK", "ETF"}
            and row.get("country") == "TW"
        ),
        None,
    )
    if not profile:
        raise ValueError(f"Could not find stock profile for {symbol}.")

    return {
        "name": profile.get("name") or symbol,
        "asset_type": "ETF" if str(profile.get("type") or "").upper() == "ETF" else "STOCK",
        "market": profile.get("market") or "TWSE",
    }


def _fetch_wantgoo_quote(session: requests.Session, symbol: str, base_url: str) -> dict:
    rows = _get_cached_json(
        session,
        f"{base_url.rstrip('/')}/investrue/all-quote-info",
        ttl_seconds=WANTGOO_QUOTE_CACHE_SECONDS,
    )
    quote = next((row for row in rows if isinstance(row, dict) and row.get("id") == symbol), None)
    if not quote:
        raise ValueError(f"Could not find quote for {symbol}.")

    close = _optional_decimal(quote.get("close"))
    open_price = _optional_decimal(quote.get("open"))
    flat = _optional_decimal(quote.get("flat"))
    high = _optional_decimal(quote.get("high"))
    low = _optional_decimal(quote.get("low"))
    timestamp = quote.get("time") or quote.get("tradeDate")
    if not any((close, open_price, flat)) or not timestamp:
        raise ValueError(f"Quote data for {symbol} is incomplete.")

    return {
        "close": close,
        "open": open_price,
        "flat": flat,
        "high": high,
        "low": low,
        "time": timestamp,
    }


def _profile_snapshot(symbol: str, profile: dict) -> StockProfileSnapshot:
    return StockProfileSnapshot(
        symbol=symbol,
        name=profile["name"],
        asset_type=profile["asset_type"],
        market=_map_market(profile["market"]),
    )


def _quote_snapshot(symbol: str, quote: dict) -> QuoteSnapshot:
    current_price = _money(quote.get("close") or quote.get("open") or quote.get("flat"))
    open_price = _optional_money(quote.get("open"))
    previous_close = _optional_money(quote.get("flat"))
    day_high = _optional_money(quote.get("high"))
    day_low = _optional_money(quote.get("low"))
    return QuoteSnapshot(
        symbol=symbol,
        open_price=open_price,
        previous_close=previous_close,
        day_high=day_high,
        day_low=day_low,
        current_price=current_price,
        change_percent=_quote_change_percent(current_price, open_price),
        price_updated_at=_timestamp_from_millis(quote["time"]),
    )


def _fetch_twse_pe(session: requests.Session, symbol: str) -> Decimal | None:
    rows = _get_json(session, f"{TWSE_OPENAPI_BASE_URL}/exchangeReport/BWIBBU_ALL")
    row = next((item for item in rows if isinstance(item, dict) and item.get("Code") == symbol), None)
    if not row:
        return None

    return _optional_money(row.get("PEratio"))


def derive_pe(current_price: Decimal, eps_rows: list[EpsSnapshot]) -> Decimal:
    ttm = next((row.eps_value for row in eps_rows if row.eps_type == "TTM"), None)
    if not ttm:
        raise ValueError("Could not derive P/E without TTM EPS.")

    return (current_price / ttm).quantize(MONEY, rounding=ROUND_HALF_UP)


def _quote_change_percent(current_price: Decimal, open_price: Decimal | None) -> Decimal | None:
    if open_price is None or open_price == 0:
        return None
    return ((current_price - open_price) / open_price * Decimal("100")).quantize(MONEY, rounding=ROUND_HALF_UP)


def _get_json(session: requests.Session, url: str, params: dict[str, str] | None = None):
    response = session.get(url, params=params, timeout=20)
    response.raise_for_status()
    return response.json()


def _get_cached_json(session: requests.Session, url: str, *, ttl_seconds: int):
    now = datetime.now(UTC)
    cached = _JSON_CACHE.get(url)
    if cached:
        cached_at, payload = cached
        if (now - cached_at).total_seconds() < ttl_seconds:
            return payload

    payload = _get_json(session, url)
    _JSON_CACHE[url] = (now, payload)
    return payload


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


def _optional_decimal(value) -> Decimal | None:
    if value in (None, "", "--", "-", "N/A"):
        return None

    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        return None


def _timestamp_from_millis(value) -> datetime:
    timestamp = float(value) / 1000
    return datetime.fromtimestamp(timestamp, tz=UTC).astimezone(TAIPEI_TZ)


def _map_market(market: str) -> str:
    return {
        "Listed": "TWSE",
        "OTC": "TPEX",
    }.get(market, market or "TWSE")
