from __future__ import annotations

import json
import random
import ssl
import string
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

import certifi
import requests
from sqlalchemy import delete, select
from websockets.sync.client import connect

from .db.models import (
    FuturesIntradayPoint,
    FuturesSnapshot,
)
from .db.session import SessionLocal
from .db.apply import log_crawler_result


TAIPEI_TZ = ZoneInfo("Asia/Taipei")
TAIFEX_SOCKJS_URL = "wss://mis.taifex.com.tw/futures/rt/000/{session_id}/websocket"
TAIFEX_CHART_DATA_URL = "https://mis.taifex.com.tw/futures/api/getChartData1M_mem"
TAIFEX_ORIGIN = "https://mis.taifex.com.tw"
TAIFEX_REFERER = "https://mis.taifex.com.tw/futures/"
YAHOO_WTX_CHART_URL = (
    "https://tw.stock.yahoo.com/_td-stock/api/resource/"
    "FinanceChartService.ApacLibraCharts;symbols=%5B%22WTX%26%22%5D;type=tick"
)
YAHOO_WTX_REFERER = "https://tw.stock.yahoo.com/future/WTX%26"
YAHOO_WTX_SOURCE = "Yahoo FinanceChartService.ApacLibraCharts WTX&"
WTX_SYMBOL = "WTX&"
WTX_NAME = "台指期近一"
WTX_SOURCE = "TAIFEX MIS rtCore WTX&"
DAY_SESSION_START = time(8, 45)
DAY_SESSION_END = time(13, 45)
NIGHT_SESSION_START = time(15, 0)
NIGHT_SESSION_END = time(5, 0)
STALE_AFTER_SECONDS = 180
BACKFILL_GAP_SECONDS = 120
YAHOO_FALLBACK_MIN_INTERVAL_SECONDS = 60
MONEY = Decimal("0.01")
PERCENT = Decimal("0.01")


@dataclass(frozen=True)
class FuturesSession:
    session_type: str
    session_label: str
    session_date: date | None


@dataclass(frozen=True)
class FuturesQuoteSnapshot:
    symbol: str
    name: str
    current_price: Decimal
    open_price: Decimal
    price_updated_at: datetime
    source: str = WTX_SOURCE
    source_symbol: str | None = None

    @property
    def difference_points(self) -> Decimal:
        return (self.current_price - self.open_price).quantize(MONEY, rounding=ROUND_HALF_UP)

    @property
    def difference_percent(self) -> Decimal:
        if self.open_price == 0:
            return Decimal("0.00")
        return ((self.current_price - self.open_price) / self.open_price * Decimal("100")).quantize(
            PERCENT,
            rounding=ROUND_HALF_UP,
        )


def current_futures_session(now: datetime | None = None) -> FuturesSession:
    local_now = _to_taipei(now or datetime.now(UTC))
    current_time = local_now.time().replace(tzinfo=None)
    if DAY_SESSION_START <= current_time < DAY_SESSION_END:
        return FuturesSession("day", "日盤", local_now.date())
    if current_time >= NIGHT_SESSION_START:
        return FuturesSession("night", "夜盤", local_now.date())
    if current_time < NIGHT_SESSION_END:
        return FuturesSession("night", "夜盤", local_now.date() - timedelta(days=1))
    return FuturesSession("closed", "最近一盤", None)


def futures_session_range(session_type: str, session_date: date | None) -> tuple[datetime | None, datetime | None]:
    if session_date is None:
        return None, None
    if session_type == "day":
        start = datetime.combine(session_date, DAY_SESSION_START, tzinfo=TAIPEI_TZ)
        end = datetime.combine(session_date, DAY_SESSION_END, tzinfo=TAIPEI_TZ)
        return start.astimezone(UTC), end.astimezone(UTC)
    if session_type == "night":
        start = datetime.combine(session_date, NIGHT_SESSION_START, tzinfo=TAIPEI_TZ)
        end = datetime.combine(session_date + timedelta(days=1), NIGHT_SESSION_END, tzinfo=TAIPEI_TZ)
        return start.astimezone(UTC), end.astimezone(UTC)
    return None, None


def refresh_wtx_futures_cache() -> FuturesQuoteSnapshot | None:
    started_at = datetime.now(UTC)
    try:
        snapshot = fetch_taifex_futures_quote(WTX_SYMBOL)
    except Exception as exc:
        _log_futures_failure(str(exc), started_at)
        try:
            snapshot = fetch_yahoo_wtx_quote_snapshot()
        except Exception as fallback_exc:
            _log_futures_failure(f"Yahoo WTX snapshot fallback failed: {fallback_exc}", started_at)
            return None

    with SessionLocal() as session:
        apply_futures_snapshot(session, snapshot)
        try:
            backfill_futures_intraday_points(session, snapshot)
        except Exception as exc:
            _log_futures_failure(f"WTX chart backfill failed: {exc}", started_at)
        apply_futures_snapshot(session, snapshot)
        session.commit()
    return snapshot


def apply_futures_snapshot(session, snapshot: FuturesQuoteSnapshot, *, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    local_time = _to_taipei(snapshot.price_updated_at)
    fetch_session = current_futures_session(now)
    futures_session = fetch_session if fetch_session.session_type != "closed" else current_futures_session(snapshot.price_updated_at)
    session_date = futures_session.session_date or local_time.date()
    point_time = snapshot.price_updated_at.replace(second=0, microsecond=0)

    row = session.scalar(select(FuturesSnapshot).where(FuturesSnapshot.symbol == snapshot.symbol))
    if not row:
        row = FuturesSnapshot(
            symbol=snapshot.symbol,
            name=snapshot.name,
            session_type=futures_session.session_type,
            session_label=futures_session.session_label,
            session_date=session_date,
            current_price=snapshot.current_price,
            open_price=snapshot.open_price,
            difference_points=snapshot.difference_points,
            difference_percent=snapshot.difference_percent,
            price_updated_at=snapshot.price_updated_at,
            source=snapshot.source,
            fetched_at=now,
        )
        session.add(row)
    else:
        row.name = snapshot.name
        row.session_type = futures_session.session_type
        row.session_label = futures_session.session_label
        row.session_date = session_date
        row.current_price = snapshot.current_price
        row.open_price = snapshot.open_price
        row.difference_points = snapshot.difference_points
        row.difference_percent = snapshot.difference_percent
        row.price_updated_at = snapshot.price_updated_at
        row.source = snapshot.source
        row.fetched_at = now
        row.updated_at = now

    if futures_session.session_type != "closed":
        _upsert_futures_intraday_point(
            session,
            snapshot=snapshot,
            futures_session=futures_session,
            session_date=session_date,
            point_time=point_time,
            price=snapshot.current_price,
            difference_percent=snapshot.difference_percent,
            source=snapshot.source,
            now=now,
        )

        heartbeat_time = _as_utc(now).replace(second=0, microsecond=0)
        quote_age_seconds = (_as_utc(now) - _as_utc(snapshot.price_updated_at)).total_seconds()
        if (
            fetch_session.session_type != "closed"
            and heartbeat_time > _as_utc(point_time)
            and 0 <= quote_age_seconds <= STALE_AFTER_SECONDS
        ):
            _upsert_futures_intraday_point(
                session,
                snapshot=snapshot,
                futures_session=fetch_session,
                session_date=fetch_session.session_date or session_date,
                point_time=heartbeat_time,
                price=snapshot.current_price,
                difference_percent=snapshot.difference_percent,
                source=f"{snapshot.source} heartbeat",
                now=now,
            )

    cutoff = session_date - timedelta(days=14)
    session.execute(delete(FuturesIntradayPoint).where(FuturesIntradayPoint.session_date < cutoff))
    session.flush()


def _upsert_futures_intraday_point(
    session,
    *,
    snapshot: FuturesQuoteSnapshot,
    futures_session: FuturesSession,
    session_date: date,
    point_time: datetime,
    price: Decimal,
    difference_percent: Decimal,
    source: str,
    now: datetime,
) -> None:
    normalized_time = _as_utc(point_time).replace(second=0, microsecond=0)
    point = session.scalar(
        select(FuturesIntradayPoint).where(
            FuturesIntradayPoint.symbol == snapshot.symbol,
            FuturesIntradayPoint.session_type == futures_session.session_type,
            FuturesIntradayPoint.session_date == session_date,
            FuturesIntradayPoint.point_time == normalized_time,
        )
    )
    if not point:
        point = FuturesIntradayPoint(
            symbol=snapshot.symbol,
            session_type=futures_session.session_type,
            session_date=session_date,
            point_time=normalized_time,
        )
        session.add(point)
    point.price = price
    point.open_price = snapshot.open_price
    point.difference_percent = difference_percent
    point.source = source
    point.fetched_at = now
    point.updated_at = now


def backfill_futures_intraday_points(session, snapshot: FuturesQuoteSnapshot) -> int:
    fetch_session = current_futures_session(snapshot.price_updated_at)
    now = datetime.now(UTC)
    count = 0

    if fetch_session.session_type == "closed" or fetch_session.session_date is None:
        if not _yahoo_fallback_allowed(session, now=now):
            return 0
        try:
            yahoo_points = fetch_yahoo_wtx_chart_points(snapshot.open_price)
            for futures_session, session_points in _group_points_by_futures_session(yahoo_points).items():
                count += apply_futures_chart_points(
                    session,
                    snapshot,
                    session_points,
                    futures_session=futures_session,
                    source=YAHOO_WTX_SOURCE,
                    overwrite_existing=False,
                )
        except Exception as exc:
            _record_futures_failure(
                session,
                f"WTX Yahoo closed-session chart fallback failed: {exc}",
                started_at=now,
            )
        return count

    official_symbol = snapshot.source_symbol or _source_symbol_from_snapshot(snapshot)
    if official_symbol and not (snapshot.source or "").startswith(YAHOO_WTX_SOURCE):
        try:
            points = fetch_taifex_chart_points(
                official_symbol,
                session_type=fetch_session.session_type,
                session_date=fetch_session.session_date,
                open_price=snapshot.open_price,
            )
            count += apply_futures_chart_points(
                session,
                snapshot,
                points,
                futures_session=fetch_session,
                source=f"{WTX_SOURCE} chart ({official_symbol})",
            )
        except Exception as exc:
            _record_futures_failure(
                session,
                f"WTX TAIFEX chart backfill failed: {exc}",
                started_at=now,
            )

    if _futures_backfill_needed(session, fetch_session, now=now) and _yahoo_fallback_allowed(session, now=now):
        try:
            yahoo_points = fetch_yahoo_wtx_chart_points(snapshot.open_price)
            count += apply_futures_chart_points(
                session,
                snapshot,
                yahoo_points,
                futures_session=fetch_session,
                source=YAHOO_WTX_SOURCE,
                overwrite_existing=False,
            )
        except Exception as exc:
            _record_futures_failure(
                session,
                f"WTX Yahoo chart fallback failed: {exc}",
                started_at=now,
            )

    return count


def apply_futures_chart_points(
    session,
    snapshot: FuturesQuoteSnapshot,
    points: list[tuple[datetime, Decimal, Decimal]],
    *,
    futures_session: FuturesSession,
    source: str | None = None,
    overwrite_existing: bool = True,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now(UTC)
    if futures_session.session_type == "closed" or futures_session.session_date is None:
        return 0

    normalized_points: dict[datetime, tuple[Decimal, Decimal]] = {}
    for point_time, price, difference_percent in points:
        normalized_time = _as_utc(point_time).replace(second=0, microsecond=0)
        if not _time_matches_session(normalized_time, futures_session):
            continue
        normalized_points[normalized_time] = (price, difference_percent)

    point_source = source or f"{WTX_SOURCE} chart ({snapshot.source_symbol or _source_symbol_from_snapshot(snapshot) or snapshot.symbol})"
    count = 0
    for normalized_time, (price, difference_percent) in normalized_points.items():
        point = session.scalar(
            select(FuturesIntradayPoint).where(
                FuturesIntradayPoint.symbol == snapshot.symbol,
                FuturesIntradayPoint.session_type == futures_session.session_type,
                FuturesIntradayPoint.session_date == futures_session.session_date,
                FuturesIntradayPoint.point_time == normalized_time,
            )
        )
        if point and not overwrite_existing:
            continue
        if not point:
            point = FuturesIntradayPoint(
                symbol=snapshot.symbol,
                session_type=futures_session.session_type,
                session_date=futures_session.session_date,
                point_time=normalized_time,
            )
            session.add(point)
        point.price = price
        point.open_price = snapshot.open_price
        point.difference_percent = difference_percent
        point.source = point_source
        point.fetched_at = now
        point.updated_at = now
        count += 1

    if count:
        session.flush()
    return count


def fetch_taifex_futures_quote(symbol: str = WTX_SYMBOL, timeout_seconds: int = 12) -> FuturesQuoteSnapshot:
    if symbol != WTX_SYMBOL:
        return _fetch_official_taifex_symbol(symbol, timeout_seconds=timeout_seconds)

    errors: list[str] = []
    for official_symbol in official_txf_candidate_symbols():
        try:
            return _fetch_official_taifex_symbol(
                official_symbol,
                timeout_seconds=timeout_seconds,
                display_symbol=WTX_SYMBOL,
                display_name=WTX_NAME,
            )
        except Exception as exc:
            errors.append(f"{official_symbol}: {exc}")
    raise ValueError("TAIFEX returned no usable TXF near-month quote. " + "; ".join(errors[:6]))


def official_txf_candidate_symbols(now: datetime | None = None) -> list[str]:
    local_now = _to_taipei(now or datetime.now(UTC))
    session = current_futures_session(local_now)
    suffixes = ["M"] if session.session_type == "night" else ["F"]
    if session.session_type == "closed":
        local_time = local_now.time().replace(tzinfo=None)
        suffixes = ["M"] if local_time < DAY_SESSION_START else ["F"]

    start_year, start_month = _near_contract_month(local_now.date())
    candidates: list[str] = []
    for month_offset in range(0, 8):
        year, month = _add_months(start_year, start_month, month_offset)
        month_code = "ABCDEFGHIJKL"[month - 1]
        year_digit = str(year % 10)
        for suffix in suffixes:
            candidates.append(f"TXF{month_code}{year_digit}-{suffix}")
    return candidates


def _fetch_official_taifex_symbol(
    official_symbol: str,
    *,
    timeout_seconds: int,
    display_symbol: str | None = None,
    display_name: str | None = None,
) -> FuturesQuoteSnapshot:
    session_id = _random_session_id()
    url = TAIFEX_SOCKJS_URL.format(session_id=session_id)
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    subscribe_payload = json.dumps({"type": "subscribe", "symbols": [official_symbol]}, separators=(",", ":"))

    with connect(
        url,
        origin=TAIFEX_ORIGIN,
        ssl=ssl_context,
        open_timeout=timeout_seconds,
        close_timeout=2,
        user_agent_header="Mozilla/5.0",
    ) as websocket:
        opened = websocket.recv(timeout=timeout_seconds)
        if opened != "o":
            raise ValueError(f"TAIFEX SockJS did not open correctly: {opened!r}")
        websocket.send(json.dumps([subscribe_payload], separators=(",", ":")))

        deadline = datetime.now(UTC) + timedelta(seconds=timeout_seconds)
        last_error: Exception | None = None
        while datetime.now(UTC) < deadline:
            try:
                frame = websocket.recv(timeout=3)
            except TimeoutError as exc:
                last_error = exc
                continue
            for message in _sockjs_messages(frame):
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    continue
                if payload.get("type") != "quote":
                    continue
                try:
                    return parse_taifex_quote_payload(
                        payload,
                        expected_symbol=official_symbol,
                        display_symbol=display_symbol or official_symbol,
                        display_name=display_name or official_symbol,
                    )
                except ValueError as exc:
                    last_error = exc
                    continue

    raise ValueError(f"TAIFEX returned no usable {official_symbol} quote. Last error: {last_error}")


def parse_taifex_quote_payload(
    payload: dict,
    *,
    expected_symbol: str = WTX_SYMBOL,
    display_symbol: str = WTX_SYMBOL,
    display_name: str = WTX_NAME,
) -> FuturesQuoteSnapshot:
    quote = payload.get("quote") or payload
    values = quote.get("values") if isinstance(quote, dict) else None
    true_values = quote.get("trueValues") if isinstance(quote, dict) else None
    merged = {
        **(quote if isinstance(quote, dict) else {}),
        **(true_values if isinstance(true_values, dict) else {}),
        **(values if isinstance(values, dict) else {}),
    }
    if not isinstance(merged, dict):
        raise ValueError("TAIFEX quote payload is not an object.")

    symbol = _first_text(merged, "55", "SymbolID", "symbol", "Symbol", "sid") or expected_symbol
    if expected_symbol and symbol != expected_symbol:
        raise ValueError(f"TAIFEX quote symbol {symbol!r} did not match {expected_symbol!r}.")

    current_price = _first_decimal(
        merged,
        "CLastPrice",
        "125",
        "LastPrice",
        "MatchPrice",
        "CTestPrice",
        "257",
        "z",
        "price",
        "last",
    )
    open_price = _first_decimal(
        merged,
        "COpenPrice",
        "126",
        "OpenPrice",
        "OpeningPrice",
        "134",
        "o",
        "open",
    )
    if current_price is None:
        raise ValueError("TAIFEX quote has no current price.")
    if open_price is None:
        raise ValueError("TAIFEX quote has no open price.")

    return FuturesQuoteSnapshot(
        symbol=display_symbol,
        name=display_name,
        current_price=current_price,
        open_price=open_price,
        price_updated_at=_quote_datetime(merged),
        source=f"{WTX_SOURCE} ({symbol})",
        source_symbol=symbol,
    )


def fetch_taifex_chart_points(
    official_symbol: str,
    *,
    session_type: str,
    session_date: date,
    open_price: Decimal,
    timeout_seconds: int = 12,
) -> list[tuple[datetime, Decimal, Decimal]]:
    response = requests.post(
        TAIFEX_CHART_DATA_URL,
        json={"SymbolID": official_symbol},
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": TAIFEX_ORIGIN,
            "Referer": TAIFEX_REFERER,
            "User-Agent": "Mozilla/5.0",
        },
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    if str(payload.get("RtCode")) != "0":
        raise ValueError(payload.get("RtMsg") or f"TAIFEX chart API returned RtCode {payload.get('RtCode')}")
    ticks = ((payload.get("RtData") or {}).get("Ticks") or [])
    return parse_taifex_chart_ticks(ticks, session_type=session_type, session_date=session_date, open_price=open_price)


def fetch_yahoo_wtx_chart_points(
    open_price: Decimal,
    timeout_seconds: int = 12,
) -> list[tuple[datetime, Decimal, Decimal]]:
    points = parse_yahoo_wtx_chart_payload(_fetch_yahoo_wtx_chart_payload(timeout_seconds), open_price=open_price)
    if not points:
        raise ValueError("Yahoo WTX chart returned no usable points.")
    return points


def fetch_yahoo_wtx_quote_snapshot(timeout_seconds: int = 12) -> FuturesQuoteSnapshot:
    return parse_yahoo_wtx_quote_snapshot(_fetch_yahoo_wtx_chart_payload(timeout_seconds))


def _fetch_yahoo_wtx_chart_payload(timeout_seconds: int) -> dict:
    response = requests.get(
        YAHOO_WTX_CHART_URL,
        params={
            "device": "desktop",
            "ecma": "modern",
            "intl": "tw",
            "lang": "zh-Hant-TW",
            "partner": "none",
            "region": "TW",
            "site": "finance",
            "tz": "Asia/Taipei",
            "ver": "1.4.886",
            "returnMeta": "true",
        },
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": YAHOO_WTX_REFERER,
            "User-Agent": "Mozilla/5.0",
        },
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    return response.json()


def parse_yahoo_wtx_quote_snapshot(payload: dict) -> FuturesQuoteSnapshot:
    chart = _yahoo_chart_from_payload(payload)
    meta = chart.get("meta") or {}
    quote = (((chart.get("indicators") or {}).get("quote") or [{}])[0] or {})
    timestamps = chart.get("timestamp") or []
    closes = quote.get("close") or []
    opens = quote.get("open") or []

    current_price = _optional_decimal(meta.get("regularMarketPrice"))
    price_updated_at: datetime | None = None
    regular_market_time = meta.get("regularMarketTime")
    if regular_market_time is not None:
        try:
            price_updated_at = datetime.fromtimestamp(int(regular_market_time), UTC)
        except (TypeError, ValueError, OSError):
            price_updated_at = None

    if current_price is None:
        for index in range(min(len(timestamps), len(closes)) - 1, -1, -1):
            current_price = _optional_decimal(closes[index])
            if current_price is None:
                continue
            try:
                price_updated_at = datetime.fromtimestamp(int(timestamps[index]), UTC)
            except (TypeError, ValueError, OSError):
                price_updated_at = datetime.now(UTC)
            break

    open_price = None
    if isinstance(opens, list):
        for value in opens:
            open_price = _optional_decimal(value)
            if open_price is not None:
                break

    if current_price is None:
        raise ValueError("Yahoo WTX chart has no current price.")
    if open_price is None:
        raise ValueError("Yahoo WTX chart has no open price.")

    return FuturesQuoteSnapshot(
        symbol=WTX_SYMBOL,
        name=WTX_NAME,
        current_price=current_price,
        open_price=open_price,
        price_updated_at=price_updated_at or datetime.now(UTC),
        source=YAHOO_WTX_SOURCE,
        source_symbol=WTX_SYMBOL,
    )


def parse_yahoo_wtx_chart_payload(
    payload: dict,
    *,
    open_price: Decimal,
) -> list[tuple[datetime, Decimal, Decimal]]:
    chart = _yahoo_chart_from_payload(payload)
    timestamps = chart.get("timestamp") or []
    quote = (((chart.get("indicators") or {}).get("quote") or [{}])[0] or {})
    closes = quote.get("close") or []
    if not isinstance(timestamps, list) or not isinstance(closes, list):
        raise ValueError("Yahoo WTX chart payload has invalid timestamp or close arrays.")

    parsed: list[tuple[datetime, Decimal, Decimal]] = []
    for index, timestamp in enumerate(timestamps):
        if index >= len(closes):
            break
        close_price = _optional_decimal(closes[index])
        if close_price is None:
            continue
        try:
            point_time = datetime.fromtimestamp(int(timestamp), UTC).replace(second=0, microsecond=0)
        except (TypeError, ValueError, OSError):
            continue
        difference_percent = Decimal("0.00")
        if open_price > 0:
            difference_percent = ((close_price - open_price) / open_price * Decimal("100")).quantize(
                PERCENT,
                rounding=ROUND_HALF_UP,
            )
        parsed.append((point_time, close_price, difference_percent))
    return parsed


def _yahoo_chart_from_payload(payload: dict) -> dict:
    data = payload.get("data") if isinstance(payload, dict) else None
    chart = ((data or [{}])[0] or {}).get("chart") if isinstance(data, list) else None
    if not isinstance(chart, dict):
        raise ValueError("Yahoo WTX chart payload has no chart object.")
    return chart


def parse_taifex_chart_ticks(
    ticks: list,
    *,
    session_type: str,
    session_date: date,
    open_price: Decimal,
) -> list[tuple[datetime, Decimal, Decimal]]:
    parsed: list[tuple[datetime, Decimal, Decimal]] = []
    for tick in ticks:
        if not isinstance(tick, list) or len(tick) < 5:
            continue
        point_time = _chart_tick_time(str(tick[0]), session_type=session_type, session_date=session_date)
        close_price = _optional_decimal(tick[4])
        if point_time is None or close_price is None:
            continue
        difference_percent = Decimal("0.00")
        if open_price > 0:
            difference_percent = ((close_price - open_price) / open_price * Decimal("100")).quantize(
                PERCENT,
                rounding=ROUND_HALF_UP,
            )
        parsed.append((point_time, close_price, difference_percent))
    return parsed


def latest_wtx_response(limit: int = 900, *, now: datetime | None = None, session_factory=None) -> dict:
    now = now or datetime.now(UTC)
    futures_session = current_futures_session(now)
    session_factory = session_factory or SessionLocal
    with session_factory() as session:
        snapshot = session.scalar(select(FuturesSnapshot).where(FuturesSnapshot.symbol == WTX_SYMBOL))
        chart_session = futures_session
        if futures_session.session_type == "closed":
            latest_chart_session = _latest_non_closed_chart_session(session)
            if latest_chart_session:
                chart_session = latest_chart_session
            elif snapshot and snapshot.session_type in {"day", "night"}:
                chart_session = FuturesSession(snapshot.session_type, snapshot.session_label, snapshot.session_date)

        if snapshot:
            session_type = chart_session.session_type
            session_date = chart_session.session_date
            points = session.scalars(
                select(FuturesIntradayPoint)
                .where(
                    FuturesIntradayPoint.symbol == WTX_SYMBOL,
                    FuturesIntradayPoint.session_type == session_type,
                    FuturesIntradayPoint.session_date == session_date,
                )
                .order_by(FuturesIntradayPoint.point_time.asc())
                .limit(limit)
            ).all()
            points = [point for point in points if _point_matches_session(point, session_type)]
        else:
            session_type = futures_session.session_type
            session_date = futures_session.session_date
            points = []
        session_start_at, session_end_at = futures_session_range(session_type, session_date)

        return {
            "symbol": WTX_SYMBOL,
            "name": WTX_NAME,
            "session_type": session_type,
            "session_label": chart_session.session_label,
            "session_start_at": session_start_at,
            "session_end_at": session_end_at,
            "current_price": _optional_float(snapshot.current_price if snapshot else None),
            "open_price": _optional_float(snapshot.open_price if snapshot else None),
            "difference_points": _optional_float(snapshot.difference_points if snapshot else None),
            "difference_percent": _optional_float(snapshot.difference_percent if snapshot else None),
            "price_updated_at": _as_utc(snapshot.price_updated_at) if snapshot else None,
            "is_stale": True if not snapshot else _is_stale(snapshot.price_updated_at, now),
            "chart_points": [
                {
                    "timestamp": _as_utc(point.point_time),
                    "price": float(point.price),
                    "difference_percent": float(point.difference_percent),
                    "source": point.source,
                }
                for point in points
            ],
        }


def _source_symbol_from_snapshot(snapshot: FuturesQuoteSnapshot) -> str | None:
    source = snapshot.source or ""
    start = source.rfind("(")
    end = source.rfind(")")
    if start >= 0 and end > start:
        return source[start + 1 : end] or None
    return None


def _chart_tick_time(value: str, *, session_type: str, session_date: date) -> datetime | None:
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) < 4:
        return None
    hour = int(digits[0:2])
    minute = int(digits[2:4])
    second = int(digits[4:6]) if len(digits) >= 6 else 0
    local_date = session_date
    if session_type == "night" and hour < NIGHT_SESSION_END.hour:
        local_date = session_date + timedelta(days=1)
    return datetime.combine(local_date, time(hour, minute, second), tzinfo=TAIPEI_TZ).astimezone(UTC)


def _sockjs_messages(frame: str) -> list[str]:
    if frame in {"o", "h"}:
        return []
    if not frame:
        return []
    prefix = frame[0]
    payload = frame[1:]
    if prefix == "a":
        parsed = json.loads(payload)
        return parsed if isinstance(parsed, list) else []
    if prefix == "m":
        return [payload]
    return []


def _point_matches_session(point: FuturesIntradayPoint, session_type: str) -> bool:
    if session_type not in {"day", "night"}:
        return True
    source = point.source or ""
    if "(TXF" not in source:
        return True
    return source.endswith("-F)") if session_type == "day" else source.endswith("-M)")


def _time_matches_session(point_time: datetime, futures_session: FuturesSession) -> bool:
    start_at, end_at = futures_session_range(futures_session.session_type, futures_session.session_date)
    if start_at is None or end_at is None:
        return True
    normalized_time = _as_utc(point_time)
    return start_at <= normalized_time <= end_at


def _futures_backfill_needed(
    session,
    futures_session: FuturesSession,
    *,
    now: datetime,
    gap_seconds: int = BACKFILL_GAP_SECONDS,
) -> bool:
    if futures_session.session_type == "closed" or futures_session.session_date is None:
        return False

    start_at, end_at = futures_session_range(futures_session.session_type, futures_session.session_date)
    if start_at is None or end_at is None:
        return False

    effective_end = min(_as_utc(now), end_at)
    if effective_end <= start_at:
        return False

    point_times = [
        _as_utc(point_time)
        for point_time in session.scalars(
            select(FuturesIntradayPoint.point_time)
            .where(
                FuturesIntradayPoint.symbol == WTX_SYMBOL,
                FuturesIntradayPoint.session_type == futures_session.session_type,
                FuturesIntradayPoint.session_date == futures_session.session_date,
            )
            .order_by(FuturesIntradayPoint.point_time.asc())
        ).all()
    ]
    point_times = [point_time for point_time in point_times if start_at <= point_time <= end_at]
    if not point_times:
        return True

    if (point_times[0] - start_at).total_seconds() > gap_seconds:
        return True
    for previous_time, current_time in zip(point_times, point_times[1:]):
        if (current_time - previous_time).total_seconds() > gap_seconds:
            return True

    current_session = current_futures_session(now)
    if (
        current_session.session_type == futures_session.session_type
        and current_session.session_date == futures_session.session_date
        and (effective_end - point_times[-1]).total_seconds() > STALE_AFTER_SECONDS
    ):
        return True
    return False


def _yahoo_fallback_allowed(session, *, now: datetime) -> bool:
    latest_fetched_at = session.scalar(
        select(FuturesIntradayPoint.fetched_at)
        .where(
            FuturesIntradayPoint.symbol == WTX_SYMBOL,
            FuturesIntradayPoint.source.like(f"{YAHOO_WTX_SOURCE}%"),
        )
        .order_by(FuturesIntradayPoint.fetched_at.desc())
        .limit(1)
    )
    if latest_fetched_at is None:
        return True
    return (_as_utc(now) - _as_utc(latest_fetched_at)).total_seconds() >= YAHOO_FALLBACK_MIN_INTERVAL_SECONDS


def _group_points_by_futures_session(
    points: list[tuple[datetime, Decimal, Decimal]],
) -> dict[FuturesSession, list[tuple[datetime, Decimal, Decimal]]]:
    grouped: dict[FuturesSession, list[tuple[datetime, Decimal, Decimal]]] = {}
    for point in points:
        futures_session = current_futures_session(point[0])
        if futures_session.session_type == "closed" or futures_session.session_date is None:
            continue
        grouped.setdefault(futures_session, []).append(point)
    return grouped


def _latest_non_closed_chart_session(session) -> FuturesSession | None:
    row = session.execute(
        select(
            FuturesIntradayPoint.session_type,
            FuturesIntradayPoint.session_date,
        )
        .where(
            FuturesIntradayPoint.symbol == WTX_SYMBOL,
            FuturesIntradayPoint.session_type.in_(("day", "night")),
        )
        .order_by(FuturesIntradayPoint.point_time.desc())
        .limit(1)
    ).first()
    if not row:
        return None
    session_type, session_date = row
    return FuturesSession(
        session_type,
        "日盤" if session_type == "day" else "夜盤",
        session_date,
    )


def _quote_datetime(values: dict) -> datetime:
    parsed_date = _first_text(values, "CDate", "Date", "date", "d")
    parsed_time = _first_text(values, "CTime", "Time", "time", "t", "258")
    if parsed_date:
        digits_date = "".join(ch for ch in parsed_date if ch.isdigit())
        digits_time = "".join(ch for ch in (parsed_time or "") if ch.isdigit())
        if len(digits_date) == 8 and len(digits_time) >= 4:
            hour = int(digits_time[0:2])
            minute = int(digits_time[2:4])
            second = int(digits_time[4:6]) if len(digits_time) >= 6 else 0
            local_dt = datetime(
                int(digits_date[0:4]),
                int(digits_date[4:6]),
                int(digits_date[6:8]),
                hour,
                minute,
                second,
                tzinfo=TAIPEI_TZ,
            )
            return local_dt.astimezone(UTC)
    if parsed_time:
        digits_time = "".join(ch for ch in parsed_time if ch.isdigit())
        if len(digits_time) >= 4:
            local_now = datetime.now(TAIPEI_TZ)
            local_dt = local_now.replace(
                hour=int(digits_time[0:2]),
                minute=int(digits_time[2:4]),
                second=int(digits_time[4:6]) if len(digits_time) >= 6 else 0,
                microsecond=0,
            )
            return local_dt.astimezone(UTC)
    return datetime.now(UTC)


def _near_contract_month(value: date) -> tuple[int, int]:
    expiry = _third_wednesday(value.year, value.month)
    if value > expiry:
        return _add_months(value.year, value.month, 1)
    return value.year, value.month


def _third_wednesday(year: int, month: int) -> date:
    current = date(year, month, 1)
    offset = (2 - current.weekday()) % 7
    return current + timedelta(days=offset + 14)


def _add_months(year: int, month: int, offset: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) + offset
    return total // 12, total % 12 + 1


def _first_decimal(values: dict, *keys: str) -> Decimal | None:
    for key in keys:
        parsed = _optional_decimal(values.get(key))
        if parsed is not None:
            return parsed
    return None


def _first_text(values: dict, *keys: str) -> str | None:
    for key in keys:
        value = values.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text and text not in {"-", "--"}:
            return text
    return None


def _optional_decimal(value) -> Decimal | None:
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if not text or text in {"-", "--", "0.0000"}:
        return None
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed.quantize(MONEY, rounding=ROUND_HALF_UP)


def _is_stale(price_updated_at: datetime, now: datetime) -> bool:
    return (now - _as_utc(price_updated_at)).total_seconds() > STALE_AFTER_SECONDS


def _log_futures_failure(message: str, started_at: datetime) -> None:
    with SessionLocal() as session:
        _record_futures_failure(session, message, started_at=started_at)
        session.commit()


def _record_futures_failure(session, message: str, *, started_at: datetime) -> None:
    log_crawler_result(
        session=session,
        job_name=f"futures_refresh:{WTX_SYMBOL}",
        status="FAILED",
        message=message,
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )


def _random_session_id(length: int = 8) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def _optional_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _to_taipei(value: datetime) -> datetime:
    return _as_utc(value).astimezone(TAIPEI_TZ)
