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
from sqlalchemy import delete, select
from websockets.sync.client import connect

from .database import (
    FuturesIntradayPoint,
    FuturesSnapshot,
    SessionLocal,
    log_crawler_result,
)


TAIPEI_TZ = ZoneInfo("Asia/Taipei")
TAIFEX_SOCKJS_URL = "wss://mis.taifex.com.tw/futures/rt/000/{session_id}/websocket"
TAIFEX_ORIGIN = "https://mis.taifex.com.tw"
WTX_SYMBOL = "WTX&"
WTX_NAME = "台指期近一"
WTX_SOURCE = "TAIFEX MIS rtCore WTX&"
DAY_SESSION_START = time(8, 45)
DAY_SESSION_END = time(13, 45)
NIGHT_SESSION_START = time(15, 0)
NIGHT_SESSION_END = time(5, 0)
STALE_AFTER_SECONDS = 180
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
        return None

    with SessionLocal() as session:
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

    point = session.scalar(
        select(FuturesIntradayPoint).where(
            FuturesIntradayPoint.symbol == snapshot.symbol,
            FuturesIntradayPoint.session_type == futures_session.session_type,
            FuturesIntradayPoint.session_date == session_date,
            FuturesIntradayPoint.point_time == point_time,
        )
    )
    if not point:
        point = FuturesIntradayPoint(
            symbol=snapshot.symbol,
            session_type=futures_session.session_type,
            session_date=session_date,
            point_time=point_time,
        )
        session.add(point)
    point.price = snapshot.current_price
    point.open_price = snapshot.open_price
    point.difference_percent = snapshot.difference_percent
    point.source = snapshot.source
    point.fetched_at = now
    point.updated_at = now

    cutoff = session_date - timedelta(days=14)
    session.execute(delete(FuturesIntradayPoint).where(FuturesIntradayPoint.session_date < cutoff))
    session.flush()


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
    )


def latest_wtx_response(limit: int = 900) -> dict:
    now = datetime.now(UTC)
    futures_session = current_futures_session(now)
    with SessionLocal() as session:
        snapshot = session.scalar(select(FuturesSnapshot).where(FuturesSnapshot.symbol == WTX_SYMBOL))
        if snapshot:
            session_type = futures_session.session_type if futures_session.session_type != "closed" else snapshot.session_type
            session_date = futures_session.session_date if futures_session.session_type != "closed" else snapshot.session_date
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
            "session_type": futures_session.session_type,
            "session_label": futures_session.session_label,
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
                }
                for point in points
            ],
        }


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
        log_crawler_result(
            session=session,
            job_name=f"futures_refresh:{WTX_SYMBOL}",
            status="FAILED",
            message=message,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )
        session.commit()


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
