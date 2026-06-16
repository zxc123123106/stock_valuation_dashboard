from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import requests
from bs4 import BeautifulSoup

from .market_data import normalize_symbol


YAHOO_STOCK_BASE_URL = "https://tw.stock.yahoo.com"
SOURCE = "Yahoo broker-trading"


@dataclass(frozen=True)
class BrokerTradingRowSnapshot:
    rank: int
    broker_name: str
    buy_volume: int
    sell_volume: int
    net_volume: int


@dataclass(frozen=True)
class BrokerTradingSnapshot:
    symbol: str
    trade_date: str
    main_net_volume: int
    main_buy_volume: int
    main_sell_volume: int
    volume_ratio_percent: Decimal | None
    buy_brokers: list[BrokerTradingRowSnapshot]
    sell_brokers: list[BrokerTradingRowSnapshot]
    source: str
    fetched_at: datetime


def fetch_broker_trading(symbol: str) -> BrokerTradingSnapshot:
    normalized_symbol = normalize_symbol(symbol)
    session = _build_session()
    response = session.get(
        f"{YAHOO_STOCK_BASE_URL}/quote/{normalized_symbol}.TW/broker-trading",
        timeout=12,
    )
    response.raise_for_status()

    return _parse_broker_trading(normalized_symbol, response.text)


def _build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 StockValuationDashboard/0.1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
        }
    )
    return session


def _parse_broker_trading(symbol: str, html: str) -> BrokerTradingSnapshot:
    soup = BeautifulSoup(html, "html.parser")
    lines = [line.strip() for line in soup.get_text("\n", strip=True).splitlines() if line.strip()]

    trade_date = _parse_trade_date(lines)
    main_net_volume = _parse_labeled_int(lines, "主力買賣超")
    main_buy_volume = _parse_labeled_int(lines, "主力買超")
    main_sell_volume = _parse_labeled_int(lines, "主力賣超")
    volume_ratio_percent = _parse_optional_percent(lines, "買賣超佔成交量")
    buy_brokers = _parse_broker_rows(lines, "買超券商", "賣超券商")
    sell_brokers = _parse_broker_rows(lines, "賣超券商", "我的自選股")

    if not buy_brokers and not sell_brokers:
        raise ValueError(f"Could not parse broker rankings for {symbol}.")

    return BrokerTradingSnapshot(
        symbol=symbol,
        trade_date=trade_date,
        main_net_volume=main_net_volume,
        main_buy_volume=main_buy_volume,
        main_sell_volume=main_sell_volume,
        volume_ratio_percent=volume_ratio_percent,
        buy_brokers=buy_brokers[:5],
        sell_brokers=sell_brokers[:5],
        source=SOURCE,
        fetched_at=datetime.now(UTC),
    )


def _parse_trade_date(lines: list[str]) -> str:
    for index, line in enumerate(lines):
        match = re.search(r"資料時間[:：]\s*(\d{4}/\d{2}/\d{2})", line)
        if match:
            return match.group(1)
        if "資料時間" in line:
            for candidate in lines[index + 1 : index + 4]:
                match = re.search(r"\d{4}/\d{2}/\d{2}", candidate)
                if match:
                    return match.group(0)
    raise ValueError("Could not parse broker trading date.")


def _parse_labeled_int(lines: list[str], label: str) -> int:
    for index, line in enumerate(lines):
        if label in line:
            inline_value = _numeric_from_labeled_line(line, label, allow_percent=False)
            if inline_value is not None:
                return _parse_int(inline_value)
            value = _next_numeric_line(lines, index + 1, allow_percent=False)
            if value is not None:
                return _parse_int(value)
    raise ValueError(f"Could not parse {label}.")


def _parse_optional_percent(lines: list[str], label: str) -> Decimal | None:
    for index, line in enumerate(lines):
        if label in line:
            inline_value = _numeric_from_labeled_line(line, label, allow_percent=True)
            if inline_value is not None:
                return _parse_decimal(inline_value.rstrip("%"))
            value = _next_numeric_line(lines, index + 1, allow_percent=True)
            if value is not None:
                return _parse_decimal(value.rstrip("%"))
    return None


def _numeric_from_labeled_line(line: str, label: str, *, allow_percent: bool) -> str | None:
    pattern = r"[+-]?\d[\d,]*(?:\.\d+)?%?" if allow_percent else r"[+-]?\d[\d,]*"
    suffix = line.split(label, 1)[1]
    match = re.search(pattern, suffix)
    return match.group(0) if match else None


def _next_numeric_line(lines: list[str], start_index: int, *, allow_percent: bool) -> str | None:
    pattern = r"^[+-]?\d[\d,]*(?:\.\d+)?%?$" if allow_percent else r"^[+-]?\d[\d,]*$"
    for line in lines[start_index : start_index + 5]:
        if re.fullmatch(pattern, line):
            return line
    return None


def _parse_broker_rows(lines: list[str], start_label: str, end_label: str) -> list[BrokerTradingRowSnapshot]:
    start_index = _find_line_index(lines, start_label)
    if start_index is None:
        return []

    end_index = _find_line_index(lines, end_label, start_index + 1)
    segment = lines[start_index + 1 : end_index] if end_index is not None else lines[start_index + 1 :]
    rows: list[BrokerTradingRowSnapshot] = []
    for line in segment:
        row = _parse_broker_row(line, len(rows) + 1)
        if row:
            rows.append(row)
        if len(rows) >= 5:
            break
    return rows or _parse_tokenized_broker_rows(segment)


def _find_line_index(lines: list[str], label: str, start_index: int = 0) -> int | None:
    for index, line in enumerate(lines[start_index:], start=start_index):
        if label in line:
            return index
    return None


def _parse_broker_row(line: str, rank: int) -> BrokerTradingRowSnapshot | None:
    match = re.match(r"^(.+?)\s+([+-]?\d[\d,]*)\s+([+-]?\d[\d,]*)\s*([+-]?\d[\d,]*)$", line)
    if not match:
        return None

    return BrokerTradingRowSnapshot(
        rank=rank,
        broker_name=match.group(1).strip(),
        buy_volume=_parse_int(match.group(2)),
        sell_volume=_parse_int(match.group(3)),
        net_volume=_parse_int(match.group(4)),
    )


def _parse_tokenized_broker_rows(lines: list[str]) -> list[BrokerTradingRowSnapshot]:
    rows: list[BrokerTradingRowSnapshot] = []
    tokens = [line for line in lines if not _is_broker_table_noise(line)]

    index = 0
    while index <= len(tokens) - 4 and len(rows) < 5:
        name = tokens[index]
        if _is_int_token(name):
            index += 1
            continue
        if (
            _is_int_token(tokens[index + 1])
            and _is_int_token(tokens[index + 2])
            and _is_int_token(tokens[index + 3])
        ):
            rows.append(
                BrokerTradingRowSnapshot(
                    rank=len(rows) + 1,
                    broker_name=name.strip(),
                    buy_volume=_parse_int(tokens[index + 1]),
                    sell_volume=_parse_int(tokens[index + 2]),
                    net_volume=_parse_int(tokens[index + 3]),
                )
            )
            index += 4
        else:
            index += 1

    return rows


def _is_broker_table_noise(line: str) -> bool:
    if line in {"券商", "買進", "賣出", "買超張數", "賣超張數", "張數", "排名"}:
        return True
    return any(
        marker in line
        for marker in (
            "買超券商",
            "賣超券商",
            "資料時間",
            "Yahoo",
            "我的自選股",
            "股市",
        )
    )


def _is_int_token(value: str) -> bool:
    return bool(re.fullmatch(r"[+-]?\d[\d,]*", value))


def _parse_int(value: str) -> int:
    return int(value.replace(",", "").replace("+", ""))


def _parse_decimal(value: str) -> Decimal:
    try:
        return Decimal(value.replace(",", "").replace("+", "")).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError(f"Could not parse decimal value {value}.") from exc
