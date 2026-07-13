from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..valuation import difference_percent, estimate_price, valuation_status
from .models import *

SUPPORTED_EPS_TYPES = ("TTM", "LAST_YEAR")


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

def apply_stock_snapshot(session: Session, snapshot) -> Stock:
    now = datetime.now(UTC)
    stock = session.scalar(select(Stock).where(Stock.symbol == snapshot.symbol))
    if not stock:
        stock = Stock(
            symbol=snapshot.symbol,
            name=snapshot.name,
            asset_type="STOCK",
            market=snapshot.market,
            currency=snapshot.currency,
            display_order=next_display_order(session),
        )
        session.add(stock)
        session.flush()

    if not stock.is_active:
        stock.display_order = next_display_order(session)

    stock.name = snapshot.name
    stock.asset_type = "STOCK"
    stock.market = snapshot.market
    stock.currency = snapshot.currency
    stock.is_active = True
    stock.updated_at = now

    session.add(
        StockMetric(
            stock_id=stock.id,
            open_price=snapshot.open_price,
            previous_close=snapshot.previous_close,
            day_high=snapshot.day_high,
            day_low=snapshot.day_low,
            current_price=snapshot.current_price,
            change_percent=snapshot.change_percent,
            current_pe=snapshot.current_pe,
            price_updated_at=snapshot.price_updated_at,
            pe_updated_at=snapshot.price_updated_at,
            source=snapshot.source,
        )
    )

    session.execute(delete(StockEPS).where(StockEPS.stock_id == stock.id))
    session.execute(delete(StockValuation).where(StockValuation.stock_id == stock.id))
    session.flush()

    for eps_snapshot in snapshot.eps_rows:
        if eps_snapshot.eps_type not in SUPPORTED_EPS_TYPES:
            continue

        eps_row = StockEPS(
            stock_id=stock.id,
            eps_type=eps_snapshot.eps_type,
            eps_value=eps_snapshot.eps_value,
            eps_period=eps_snapshot.eps_period,
            source=snapshot.source,
            eps_updated_at=snapshot.fetched_at,
        )
        session.add(eps_row)

        estimated = estimate_price(eps_snapshot.eps_value, snapshot.current_pe)
        price_difference = estimated - snapshot.current_price
        percent = difference_percent(snapshot.current_price, estimated)
        session.add(
            StockValuation(
                stock_id=stock.id,
                eps_type=eps_snapshot.eps_type,
                current_price=snapshot.current_price,
                current_pe=snapshot.current_pe,
                eps_value=eps_snapshot.eps_value,
                estimated_price=estimated,
                price_difference=price_difference,
                difference_percent=percent,
                valuation_status=valuation_status(percent),
                source=snapshot.source,
                calculated_at=snapshot.fetched_at,
            )
        )

    return stock


def apply_layered_stock_refresh(
    session: Session,
    *,
    profile,
    quote,
    current_pe: Decimal | None,
    pe_updated_at: datetime | None,
    eps_rows: list | None,
    eps_updated_at: datetime | None,
    source: str,
    calculated_at: datetime,
    pe_data_date: date | None = None,
) -> Stock:
    now = datetime.now(UTC)
    stock = session.scalar(select(Stock).where(Stock.symbol == profile.symbol))
    if not stock:
        stock = Stock(
            symbol=profile.symbol,
            name=profile.name,
            asset_type=profile.asset_type,
            market=profile.market,
            currency=profile.currency,
            display_order=next_display_order(session),
        )
        session.add(stock)
        session.flush()

    if not stock.is_active:
        stock.display_order = next_display_order(session)

    stock.name = profile.name
    stock.asset_type = profile.asset_type
    stock.market = profile.market
    stock.currency = profile.currency
    stock.is_active = True
    stock.updated_at = now

    latest_metric = session.scalar(
        select(StockMetric)
        .where(StockMetric.stock_id == stock.id)
        .order_by(StockMetric.created_at.desc())
        .limit(1)
    )
    metric_pe = _positive_pe(current_pe)
    metric_pe_updated_at = pe_updated_at
    metric_pe_data_date = pe_data_date
    pe_snapshot_received = pe_updated_at is not None or pe_data_date is not None
    if not pe_snapshot_received and latest_metric:
        metric_pe = _positive_pe(latest_metric.current_pe)
        metric_pe_updated_at = latest_metric.pe_updated_at
        metric_pe_data_date = latest_metric.pe_data_date
    stored_metric_pe = metric_pe or Decimal("0.00")
    if metric_pe_updated_at is None:
        metric_pe_updated_at = quote.price_updated_at

    session.add(
        StockMetric(
            stock_id=stock.id,
            open_price=quote.open_price,
            previous_close=quote.previous_close,
            day_high=quote.day_high,
            day_low=quote.day_low,
            current_price=quote.current_price,
            change_percent=quote.change_percent,
            current_pe=stored_metric_pe,
            pe_average_3y=latest_metric.pe_average_3y if latest_metric else None,
            pe_min_3y=latest_metric.pe_min_3y if latest_metric else None,
            pe_max_3y=latest_metric.pe_max_3y if latest_metric else None,
            pe_vs_average_percent=_pe_vs_average_percent(metric_pe, latest_metric.pe_average_3y if latest_metric else None),
            price_updated_at=quote.price_updated_at,
            pe_updated_at=metric_pe_updated_at,
            pe_data_date=metric_pe_data_date,
            source=source,
        )
    )

    if stock.asset_type == "ETF":
        session.execute(delete(StockEPS).where(StockEPS.stock_id == stock.id))
        valuation_inputs = []
    elif eps_rows is not None:
        session.execute(delete(StockEPS).where(StockEPS.stock_id == stock.id))
        session.flush()
        valuation_inputs = []
        for eps_snapshot in eps_rows:
            if eps_snapshot.eps_type not in SUPPORTED_EPS_TYPES:
                continue

            session.add(
                StockEPS(
                    stock_id=stock.id,
                    eps_type=eps_snapshot.eps_type,
                    eps_value=eps_snapshot.eps_value,
                    eps_period=eps_snapshot.eps_period,
                    source=source,
                    eps_updated_at=eps_updated_at or calculated_at,
                )
            )
            valuation_inputs.append(
                (eps_snapshot.eps_type, eps_snapshot.eps_value, eps_snapshot.eps_period)
            )
    else:
        valuation_inputs = [
            (eps_row.eps_type, eps_row.eps_value, eps_row.eps_period)
            for eps_row in session.scalars(
                select(StockEPS)
                .where(StockEPS.stock_id == stock.id)
                .order_by(StockEPS.eps_type.desc())
            ).all()
            if eps_row.eps_type in SUPPORTED_EPS_TYPES
        ]

    session.execute(delete(StockValuation).where(StockValuation.stock_id == stock.id))
    session.flush()

    if metric_pe is None:
        return stock

    for eps_type, eps_value, _ in valuation_inputs:
        if eps_value <= 0:
            continue
        estimated = estimate_price(eps_value, metric_pe)
        price_difference = estimated - quote.current_price
        percent = difference_percent(quote.current_price, estimated)
        session.add(
            StockValuation(
                stock_id=stock.id,
                eps_type=eps_type,
                current_price=quote.current_price,
                current_pe=metric_pe,
                eps_value=eps_value,
                estimated_price=estimated,
                price_difference=price_difference,
                difference_percent=percent,
                valuation_status=valuation_status(percent),
                source=source,
                calculated_at=calculated_at,
            )
        )

    return stock


def _replace_cached_valuations(
    session: Session,
    stock: Stock,
    metric: StockMetric,
    *,
    source: str,
    calculated_at: datetime,
) -> None:
    session.execute(delete(StockValuation).where(StockValuation.stock_id == stock.id))
    session.flush()
    metric_pe = _positive_pe(metric.current_pe)
    if stock.asset_type == "ETF" or metric_pe is None:
        return

    eps_rows = session.scalars(
        select(StockEPS)
        .where(StockEPS.stock_id == stock.id, StockEPS.eps_type.in_(SUPPORTED_EPS_TYPES))
        .order_by(StockEPS.eps_type.desc())
    ).all()
    for eps_row in eps_rows:
        if eps_row.eps_value <= 0:
            continue
        estimated = estimate_price(eps_row.eps_value, metric_pe)
        price_difference = estimated - metric.current_price
        percent = difference_percent(metric.current_price, estimated)
        session.add(
            StockValuation(
                stock_id=stock.id,
                eps_type=eps_row.eps_type,
                current_price=metric.current_price,
                current_pe=metric_pe,
                eps_value=eps_row.eps_value,
                estimated_price=estimated,
                price_difference=price_difference,
                difference_percent=percent,
                valuation_status=valuation_status(percent),
                source=source,
                calculated_at=calculated_at,
            )
        )


def apply_quote_refresh(
    session: Session,
    *,
    profile,
    quote,
    calculated_at: datetime,
) -> Stock:
    now = datetime.now(UTC)
    stock = session.scalar(select(Stock).where(Stock.symbol == profile.symbol))
    if not stock:
        stock = Stock(
            symbol=profile.symbol,
            name=profile.name,
            asset_type=profile.asset_type,
            market=profile.market,
            currency=profile.currency,
            display_order=next_display_order(session),
        )
        session.add(stock)
        session.flush()

    if not stock.is_active:
        stock.display_order = next_display_order(session)
    stock.name = profile.name
    stock.asset_type = profile.asset_type
    stock.market = profile.market
    stock.currency = profile.currency
    stock.is_active = True
    stock.updated_at = now

    latest_metric = session.scalar(
        select(StockMetric)
        .where(StockMetric.stock_id == stock.id)
        .order_by(StockMetric.created_at.desc())
        .limit(1)
    )
    quote_time = quote.price_updated_at
    latest_quote_time = latest_metric.price_updated_at if latest_metric else None
    if (
        latest_metric
        and latest_quote_time
        and quote_time
        and "latest close" in quote.source.lower()
        and _aware_utc(quote_time) <= _aware_utc(latest_quote_time)
    ):
        return stock
    metric = StockMetric(
        stock_id=stock.id,
        open_price=quote.open_price,
        previous_close=quote.previous_close,
        day_high=quote.day_high,
        day_low=quote.day_low,
        current_price=quote.current_price,
        change_percent=quote.change_percent,
        current_pe=latest_metric.current_pe if latest_metric else Decimal("0.00"),
        pe_average_3y=latest_metric.pe_average_3y if latest_metric else None,
        pe_min_3y=latest_metric.pe_min_3y if latest_metric else None,
        pe_max_3y=latest_metric.pe_max_3y if latest_metric else None,
        pe_vs_average_percent=latest_metric.pe_vs_average_percent if latest_metric else None,
        price_updated_at=quote.price_updated_at,
        pe_updated_at=latest_metric.pe_updated_at if latest_metric else quote.price_updated_at,
        pe_data_date=latest_metric.pe_data_date if latest_metric else None,
        source=quote.source,
    )
    session.add(metric)
    session.flush()

    if stock.asset_type == "ETF":
        session.execute(delete(StockEPS).where(StockEPS.stock_id == stock.id))
    _replace_cached_valuations(
        session,
        stock,
        metric,
        source=quote.source,
        calculated_at=calculated_at,
    )
    return stock


def apply_fundamental_refresh(
    session: Session,
    *,
    symbol: str,
    current_pe: Decimal | None,
    pe_received: bool,
    pe_updated_at: datetime | None,
    pe_data_date: date | None,
    pe_source: str | None,
    eps_rows: list | None,
    eps_updated_at: datetime | None,
    calculated_at: datetime,
) -> Stock:
    stock = session.scalar(select(Stock).where(Stock.symbol == symbol, Stock.is_active.is_(True)))
    if not stock:
        raise ValueError(f"Active stock {symbol} was not found.")
    latest_metric = session.scalar(
        select(StockMetric)
        .where(StockMetric.stock_id == stock.id)
        .order_by(StockMetric.created_at.desc())
        .limit(1)
    )
    if not latest_metric:
        raise ValueError(f"Quote cache for {symbol} is not ready.")

    metric = latest_metric
    if pe_received:
        metric_pe = _positive_pe(current_pe)
        metric = StockMetric(
            stock_id=stock.id,
            open_price=latest_metric.open_price,
            previous_close=latest_metric.previous_close,
            day_high=latest_metric.day_high,
            day_low=latest_metric.day_low,
            current_price=latest_metric.current_price,
            change_percent=latest_metric.change_percent,
            current_pe=metric_pe or Decimal("0.00"),
            pe_average_3y=latest_metric.pe_average_3y,
            pe_min_3y=latest_metric.pe_min_3y,
            pe_max_3y=latest_metric.pe_max_3y,
            pe_vs_average_percent=_pe_vs_average_percent(metric_pe, latest_metric.pe_average_3y),
            price_updated_at=latest_metric.price_updated_at,
            pe_updated_at=pe_updated_at or calculated_at,
            pe_data_date=pe_data_date,
            source=pe_source or latest_metric.source,
        )
        session.add(metric)
        session.flush()

    if stock.asset_type == "ETF":
        session.execute(delete(StockEPS).where(StockEPS.stock_id == stock.id))
    elif eps_rows is not None:
        session.execute(delete(StockEPS).where(StockEPS.stock_id == stock.id))
        session.flush()
        for snapshot in eps_rows:
            if snapshot.eps_type not in SUPPORTED_EPS_TYPES:
                continue
            session.add(
                StockEPS(
                    stock_id=stock.id,
                    eps_type=snapshot.eps_type,
                    eps_value=snapshot.eps_value,
                    eps_period=snapshot.eps_period,
                    source="FinMind TaiwanStockFinancialStatements",
                    eps_updated_at=eps_updated_at or calculated_at,
                )
            )
        session.flush()

    _replace_cached_valuations(
        session,
        stock,
        metric,
        source=pe_source or "TWSE / FinMind cached fundamentals",
        calculated_at=calculated_at,
    )
    stock.updated_at = datetime.now(UTC)
    return stock


def apply_broker_trading_snapshot(session: Session, stock: Stock, snapshot) -> None:
    now = datetime.now(UTC)
    broker_trading = session.scalar(
        select(StockBrokerTrading).where(StockBrokerTrading.stock_id == stock.id)
    )
    if not broker_trading:
        broker_trading = StockBrokerTrading(
            stock_id=stock.id,
            trade_date=snapshot.trade_date,
            main_net_volume=snapshot.main_net_volume,
            main_buy_volume=snapshot.main_buy_volume,
            main_sell_volume=snapshot.main_sell_volume,
            volume_ratio_percent=snapshot.volume_ratio_percent,
            source=snapshot.source,
            fetched_at=snapshot.fetched_at,
        )
        session.add(broker_trading)
        session.flush()
    else:
        broker_trading.trade_date = snapshot.trade_date
        broker_trading.main_net_volume = snapshot.main_net_volume
        broker_trading.main_buy_volume = snapshot.main_buy_volume
        broker_trading.main_sell_volume = snapshot.main_sell_volume
        broker_trading.volume_ratio_percent = snapshot.volume_ratio_percent
        broker_trading.source = snapshot.source
        broker_trading.fetched_at = snapshot.fetched_at
        broker_trading.updated_at = now

    session.execute(
        delete(StockBrokerTradingRow).where(StockBrokerTradingRow.broker_trading_id == broker_trading.id)
    )
    session.flush()

    for side, rows in (("BUY", snapshot.buy_brokers), ("SELL", snapshot.sell_brokers)):
        for row in rows[:5]:
            session.add(
                StockBrokerTradingRow(
                    broker_trading_id=broker_trading.id,
                    side=side,
                    rank=row.rank,
                    broker_name=row.broker_name,
                    buy_volume=row.buy_volume,
                    sell_volume=row.sell_volume,
                    net_volume=row.net_volume,
                )
            )


def apply_daily_price_snapshots(session: Session, stock: Stock, snapshots: list) -> None:
    if not snapshots:
        return

    existing_rows = {
        row.trade_date: row
        for row in session.scalars(
            select(StockDailyPrice).where(
                StockDailyPrice.stock_id == stock.id,
                StockDailyPrice.trade_date >= snapshots[0].trade_date,
            )
        ).all()
    }
    now = datetime.now(UTC)
    for snapshot in snapshots:
        row = existing_rows.get(snapshot.trade_date)
        if not row:
            row = StockDailyPrice(stock_id=stock.id, trade_date=snapshot.trade_date)
            session.add(row)
        row.open_price = snapshot.open_price
        row.high_price = snapshot.high_price
        row.low_price = snapshot.low_price
        row.close_price = snapshot.close_price
        row.volume = snapshot.volume
        row.source = snapshot.source
        row.fetched_at = snapshot.fetched_at
        row.updated_at = now


def apply_pe_history_snapshots(session: Session, stock: Stock, snapshots: list) -> None:
    if not snapshots:
        return

    existing_rows = {
        row.trade_date: row
        for row in session.scalars(
            select(StockPEHistory).where(
                StockPEHistory.stock_id == stock.id,
                StockPEHistory.trade_date >= snapshots[0].trade_date,
            )
        ).all()
    }
    now = datetime.now(UTC)
    for snapshot in snapshots:
        row = existing_rows.get(snapshot.trade_date)
        if not row:
            row = StockPEHistory(stock_id=stock.id, trade_date=snapshot.trade_date)
            session.add(row)
        row.per = snapshot.per
        row.pbr = snapshot.pbr
        row.dividend_yield = snapshot.dividend_yield
        row.source = snapshot.source
        row.fetched_at = snapshot.fetched_at
        row.updated_at = now

    _update_latest_metric_pe_summary(session, stock)


def apply_monthly_revenue_snapshots(session: Session, stock: Stock, snapshots: list) -> None:
    if not snapshots:
        return

    existing_rows = {
        row.month_date: row
        for row in session.scalars(
            select(StockMonthlyRevenue).where(
                StockMonthlyRevenue.stock_id == stock.id,
                StockMonthlyRevenue.month_date >= snapshots[0].month_date,
            )
        ).all()
    }
    now = datetime.now(UTC)
    for snapshot in snapshots:
        row = existing_rows.get(snapshot.month_date)
        if not row:
            row = StockMonthlyRevenue(stock_id=stock.id, month_date=snapshot.month_date)
            session.add(row)
        row.revenue = snapshot.revenue
        row.mom_percent = snapshot.mom_percent
        row.yoy_percent = snapshot.yoy_percent
        row.source = snapshot.source
        row.fetched_at = snapshot.fetched_at
        row.updated_at = now


def apply_financial_quarter_snapshots(session: Session, stock: Stock, snapshots: list) -> None:
    if not snapshots:
        return

    existing_rows = {
        row.quarter_date: row
        for row in session.scalars(
            select(StockFinancialQuarter).where(
                StockFinancialQuarter.stock_id == stock.id,
                StockFinancialQuarter.quarter_date >= snapshots[0].quarter_date,
            )
        ).all()
    }
    now = datetime.now(UTC)
    for snapshot in snapshots:
        row = existing_rows.get(snapshot.quarter_date)
        if not row:
            row = StockFinancialQuarter(stock_id=stock.id, quarter_date=snapshot.quarter_date)
            session.add(row)
        row.eps = snapshot.eps
        row.revenue = snapshot.revenue
        row.gross_profit = snapshot.gross_profit
        row.operating_income = snapshot.operating_income
        row.net_income = snapshot.net_income
        row.source = snapshot.source
        row.fetched_at = snapshot.fetched_at
        row.updated_at = now


def _update_latest_metric_pe_summary(session: Session, stock: Stock) -> None:
    latest_metric = session.scalar(
        select(StockMetric)
        .where(StockMetric.stock_id == stock.id)
        .order_by(StockMetric.created_at.desc())
        .limit(1)
    )
    if not latest_metric:
        return

    latest_history = session.scalar(
        select(StockPEHistory)
        .where(StockPEHistory.stock_id == stock.id)
        .order_by(StockPEHistory.trade_date.desc())
        .limit(1)
    )
    if latest_history and (
        latest_metric.pe_data_date is None
        or latest_history.trade_date > latest_metric.pe_data_date
    ):
        latest_metric.current_pe = _positive_pe(latest_history.per) or Decimal("0.00")
        latest_metric.pe_data_date = latest_history.trade_date
        latest_metric.pe_updated_at = latest_history.fetched_at

    pe_values = [
        value
        for value in session.scalars(
            select(StockPEHistory.per)
            .where(StockPEHistory.stock_id == stock.id, StockPEHistory.per.is_not(None))
            .order_by(StockPEHistory.trade_date.desc())
            .limit(365 * 3)
        ).all()
        if value is not None and value > 0
    ]
    if not pe_values:
        latest_metric.pe_average_3y = None
        latest_metric.pe_min_3y = None
        latest_metric.pe_max_3y = None
        latest_metric.pe_vs_average_percent = None
        return

    pe_average = (sum(pe_values, Decimal("0.00")) / Decimal(len(pe_values))).quantize(Decimal("0.01"))
    latest_metric.pe_average_3y = pe_average
    latest_metric.pe_min_3y = min(pe_values)
    latest_metric.pe_max_3y = max(pe_values)
    latest_metric.pe_vs_average_percent = _pe_vs_average_percent(latest_metric.current_pe, pe_average)


def backfill_latest_metric_pe_from_history(session: Session) -> None:
    stocks = session.scalars(select(Stock)).all()
    for stock in stocks:
        _update_latest_metric_pe_summary(session, stock)


def _pe_vs_average_percent(current_pe: Decimal | None, average_pe: Decimal | None) -> Decimal | None:
    if current_pe is None or current_pe <= 0 or average_pe is None or average_pe <= 0:
        return None
    return ((current_pe - average_pe) / average_pe * Decimal("100")).quantize(Decimal("0.01"))


def _positive_pe(value: Decimal | None) -> Decimal | None:
    if value is None or value <= 0:
        return None
    return value


def next_display_order(session: Session) -> int:
    current_max = session.scalar(
        select(func.max(Stock.display_order)).where(Stock.is_active.is_(True))
    ) or 0
    return int(current_max) + 10


def log_crawler_result(
    session: Session,
    job_name: str,
    status: str,
    message: str,
    started_at: datetime,
    finished_at: datetime | None = None,
) -> None:
    session.add(
        CrawlerLog(
            job_name=job_name,
            status=status,
            message=message[:500],
            started_at=started_at,
            finished_at=finished_at or datetime.now(UTC),
        )
    )



__all__ = [name for name in globals() if not name.startswith("__")]
