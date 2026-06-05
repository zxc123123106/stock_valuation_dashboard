from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from .wantgoo import EpsSnapshot, normalize_symbol


HISTOCK_BASE_URL = "https://histock.tw"
MONEY = Decimal("0.01")
SOURCE = "HiStock EPS"


@dataclass(frozen=True)
class HistockEpsTable:
    years: list[int]
    quarters: dict[tuple[int, int], Decimal]
    totals: dict[int, Decimal]


def fetch_stock_eps(symbol: str) -> list[EpsSnapshot]:
    normalized_symbol = normalize_symbol(symbol)
    session = _build_session()
    path = quote("每股盈餘")
    response = session.get(f"{HISTOCK_BASE_URL}/stock/{normalized_symbol}/{path}", timeout=15)
    response.raise_for_status()
    return _parse_eps_page(normalized_symbol, response.text)


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


def _parse_eps_page(symbol: str, html: str) -> list[EpsSnapshot]:
    table = _parse_eps_table(symbol, html)
    if len(table.quarters) < 4:
        raise ValueError(f"Could not find at least four quarterly EPS rows for {symbol}.")

    latest_four = sorted(table.quarters.items(), key=lambda item: item[0])[-4:]
    ttm_value = sum((eps for _, eps in latest_four), Decimal("0.00")).quantize(MONEY, rounding=ROUND_HALF_UP)
    ttm_period = " + ".join(_quarter_label(quarter) for quarter, _ in reversed(latest_four))

    complete_year = _latest_complete_year(table)
    if complete_year is None:
        raise ValueError(f"Could not find a complete fiscal year EPS set for {symbol}.")

    last_year_value = table.totals.get(complete_year)
    if last_year_value is None:
        last_year_value = sum(
            table.quarters[(complete_year, quarter)]
            for quarter in range(1, 5)
        ).quantize(MONEY, rounding=ROUND_HALF_UP)

    return [
        EpsSnapshot(eps_type="TTM", eps_value=ttm_value, eps_period=ttm_period),
        EpsSnapshot(eps_type="LAST_YEAR", eps_value=last_year_value, eps_period=str(complete_year)),
    ]


def _parse_eps_table(symbol: str, html: str) -> HistockEpsTable:
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        rows = [
            [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
            for row in table.find_all("tr")
        ]
        rows = [row for row in rows if row]
        if rows and rows[0] and rows[0][0] == "季別/年度":
            return _table_from_rows(rows)

    raise ValueError(f"Could not parse HiStock EPS table for {symbol}.")


def _table_from_rows(rows: list[list[str]]) -> HistockEpsTable:
    years = [_parse_year(value) for value in rows[0][1:]]
    quarters: dict[tuple[int, int], Decimal] = {}
    totals: dict[int, Decimal] = {}

    for row in rows[1:]:
        label = row[0]
        values = row[1:]
        if label in {"Q1", "Q2", "Q3", "Q4"}:
            quarter = int(label[1])
            for year, raw_value in zip(years, values):
                value = _optional_money(raw_value)
                if value is not None:
                    quarters[(year, quarter)] = value
        elif label == "總計":
            for year, raw_value in zip(years, values):
                value = _optional_money(raw_value)
                if value is not None:
                    totals[year] = value

    return HistockEpsTable(years=years, quarters=quarters, totals=totals)


def _latest_complete_year(table: HistockEpsTable) -> int | None:
    for year in sorted(table.years, reverse=True):
        if all((year, quarter) in table.quarters for quarter in range(1, 5)):
            return year
    return None


def _parse_year(value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Could not parse HiStock EPS year {value!r}.") from exc


def _optional_money(value: str) -> Decimal | None:
    if value in {"", "-", "--", "N/A"}:
        return None
    try:
        return Decimal(value.replace(",", "").strip()).quantize(MONEY, rounding=ROUND_HALF_UP)
    except (InvalidOperation, AttributeError):
        return None


def _quarter_label(quarter: tuple[int, int]) -> str:
    return f"{quarter[0]}Q{quarter[1]}"


def fetched_at() -> datetime:
    return datetime.now(UTC)
