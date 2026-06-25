from __future__ import annotations

import csv
import io
import json
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from .brokers import BrokerConfig, broker_options, get_broker, transaction_tax_rate
from .ai_analysis import (
    AI_MODE_HELD,
    AI_MODE_UNHELD,
    PROMPT_VERSION,
    AIAnalysisError,
    AIConfigurationError,
    ai_provider_identity,
    build_ai_provider,
    normalize_ai_analysis,
    normalize_ai_analysis_with_errors,
    stock_summary_hash,
)
from .config import get_settings
from .database import (
    CrawlerLog,
    DATABASE_URL,
    Stock,
    StockAIAnalysis,
    StockBrokerTrading,
    StockBrokerTradingRow,
    StockDailyPrice,
    StockEPS,
    StockFinancialQuarter,
    StockMetric,
    StockMonthlyRevenue,
    StockPEHistory,
    StockPosition,
    StockRefreshState,
    StockValuation,
    get_app_setting,
    get_session,
    init_database,
    ping_database,
    set_app_setting,
)
from .schemas import (
    BrokerOptionResponse,
    BrokerSettingRequest,
    BrokerSettingResponse,
    FuturesWtxResponse,
    FundamentalTrendCategoryResponse,
    FundamentalTrendPointResponse,
    FundamentalTrendSummaryResponse,
    FundamentalTrendsResponse,
    FundamentalResponse,
    HealthResponse,
    MetadataResponse,
    BrokerTradingResponse,
    BrokerTradingRowResponse,
    RefreshQueueResponse,
    RefreshStatusResponse,
    StockDeleteResponse,
    StockAIAnalysisRequest,
    StockAIAnalysisModesResponse,
    StockAIAnalysisResultResponse,
    StockAIAnalysisResponse,
    StockMetricResponse,
    StockPositionRequest,
    StockPositionResponse,
    StockReorderRequest,
    StockResponse,
    StockValuationResponse,
    TechnicalAnalysisResponse,
    TechnicalCandleResponse,
)
from .refresh_worker import BackgroundRefreshManager
from .taifex_futures import latest_wtx_response
from .technical import MOVING_AVERAGE_PERIODS, moving_averages
from .valuation import quantize_money, valuation_status
from .market_data import normalize_symbol


settings = get_settings()
refresh_manager = BackgroundRefreshManager(
    interval_seconds=settings.background_refresh_seconds,
    finmind_token=settings.finmind_token,
)

TAIPEI_TZ = ZoneInfo("Asia/Taipei")
MARKET_OPEN_TIME = time(9, 0)
MARKET_CLOSE_TIME = time(14, 0)
AI_ANALYSIS_INFLIGHT_TIMEOUT = timedelta(minutes=10)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_database()
    await refresh_manager.start()
    try:
        yield
    finally:
        await refresh_manager.stop()


app = FastAPI(
    title="Stock Valuation Dashboard API",
    version=settings.api_version,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if "*" in settings.cors_origins else settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


def _float(value: Decimal | None) -> float:
    return float(value or Decimal("0"))


def _optional_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _optional_positive_float(value: Decimal | None) -> float | None:
    if value is None or value <= 0:
        return None
    return float(value)


def _percent(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == 0:
        return Decimal("0.00")
    return quantize_money(numerator / denominator * Decimal("100"))


def _positive_money(value: float) -> Decimal:
    try:
        parsed = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Buy price must be a positive number.") from exc
    if parsed <= 0:
        raise ValueError("Buy price must be a positive number.")
    return parsed


def _database_kind() -> str:
    return "sqlite"


def _latest_metric(session: Session, stock_id: int) -> StockMetric | None:
    return session.scalar(
        select(StockMetric)
        .where(StockMetric.stock_id == stock_id)
        .order_by(StockMetric.created_at.desc())
        .limit(1)
    )


def _active_stocks(session: Session) -> list[Stock]:
    return session.scalars(
        select(Stock)
        .where(Stock.is_active.is_(True))
        .order_by(Stock.display_order, Stock.symbol)
    ).all()


def _active_valuations_count(session: Session) -> int:
    return session.scalar(
        select(func.count())
        .select_from(StockValuation)
        .join(Stock)
        .where(Stock.is_active.is_(True))
    ) or 0


def _latest_official_data_date(session: Session) -> date | None:
    daily_date = session.scalar(
        select(func.max(StockDailyPrice.trade_date))
        .join(Stock)
        .where(Stock.is_active.is_(True))
    )
    pe_date = session.scalar(
        select(func.max(StockMetric.pe_data_date))
        .join(Stock)
        .where(Stock.is_active.is_(True))
    )
    return max((value for value in (daily_date, pe_date) if value is not None), default=None)


def _valuation_response(valuation: StockValuation, buy_price: Decimal | None = None) -> StockValuationResponse:
    eps_period = None
    if valuation.stock:
        eps_period = next(
            (eps.eps_period for eps in valuation.stock.eps_rows if eps.eps_type == valuation.eps_type),
            None,
        )

    cost_difference = None
    cost_difference_percent = None
    if buy_price is not None:
        cost_difference = valuation.estimated_price - buy_price
        cost_difference_percent = _percent(cost_difference, buy_price)

    current_difference = valuation.estimated_price - valuation.current_price
    current_difference_percent = _percent(current_difference, valuation.current_price)

    return StockValuationResponse(
        eps_type=valuation.eps_type,
        eps_value=_float(valuation.eps_value),
        eps_period=eps_period,
        estimated_price=_float(valuation.estimated_price),
        price_difference=_float(current_difference),
        difference_percent=_float(current_difference_percent),
        cost_difference=_float(cost_difference) if cost_difference is not None else None,
        cost_difference_percent=_float(cost_difference_percent) if cost_difference_percent is not None else None,
        valuation_status=valuation_status(current_difference_percent),
        source=valuation.source,
        calculated_at=valuation.calculated_at,
    )


def _selected_broker(session: Session) -> BrokerConfig:
    broker_id = get_app_setting(session, "selected_broker", "CATHAY") or "CATHAY"
    try:
        return get_broker(broker_id)
    except ValueError:
        return get_broker("CATHAY")


def _broker_option_response(broker: BrokerConfig) -> BrokerOptionResponse:
    return BrokerOptionResponse(
        broker_id=broker.broker_id,
        name=broker.name,
        buy_fee_rate=float(broker.buy_fee_rate),
        sell_fee_rate=float(broker.sell_fee_rate),
        source_url=broker.source_url,
    )


def _broker_setting_response(session: Session) -> BrokerSettingResponse:
    selected = _selected_broker(session)
    return BrokerSettingResponse(
        selected_broker=selected.broker_id,
        selected=_broker_option_response(selected),
        brokers=[_broker_option_response(broker) for broker in broker_options()],
    )


def _position_response(
    position: StockPosition | None,
    metric: StockMetric | None,
    broker: BrokerConfig,
    asset_type: str,
) -> StockPositionResponse | None:
    if not position:
        return None

    profit_loss = None
    profit_loss_percent = None
    fee_adjusted_profit_loss = None
    fee_adjusted_profit_loss_percent = None
    if metric:
        profit_loss = metric.current_price - position.buy_price
        profit_loss_percent = _percent(profit_loss, position.buy_price)

        buy_fee = position.buy_price * broker.buy_fee_rate
        sell_fee = metric.current_price * broker.sell_fee_rate
        tax_rate = transaction_tax_rate(asset_type)
        transaction_tax = metric.current_price * tax_rate
        effective_buy_cost = position.buy_price + buy_fee
        estimated_sell_proceeds = metric.current_price - sell_fee - transaction_tax
        fee_adjusted_profit_loss = quantize_money(estimated_sell_proceeds - effective_buy_cost)
        fee_adjusted_profit_loss_percent = _percent(fee_adjusted_profit_loss, effective_buy_cost)

    return StockPositionResponse(
        buy_price=_float(position.buy_price),
        unrealized_profit_loss=_float(profit_loss) if profit_loss is not None else None,
        unrealized_profit_loss_percent=_float(profit_loss_percent) if profit_loss_percent is not None else None,
        fee_adjusted_profit_loss=_float(fee_adjusted_profit_loss) if fee_adjusted_profit_loss is not None else None,
        fee_adjusted_profit_loss_percent=_float(fee_adjusted_profit_loss_percent) if fee_adjusted_profit_loss_percent is not None else None,
        broker_id=broker.broker_id,
        broker_fee_rate=float(broker.buy_fee_rate),
    )


def _broker_trading_response(stock: Stock, session: Session) -> BrokerTradingResponse | None:
    broker_trading = session.scalar(
        select(StockBrokerTrading).where(StockBrokerTrading.stock_id == stock.id)
    )
    if not broker_trading:
        return None

    rows = session.scalars(
        select(StockBrokerTradingRow)
        .where(StockBrokerTradingRow.broker_trading_id == broker_trading.id)
        .order_by(StockBrokerTradingRow.side, StockBrokerTradingRow.rank)
    ).all()

    return BrokerTradingResponse(
        trade_date=broker_trading.trade_date,
        main_net_volume=broker_trading.main_net_volume,
        main_buy_volume=broker_trading.main_buy_volume,
        main_sell_volume=broker_trading.main_sell_volume,
        volume_ratio_percent=float(broker_trading.volume_ratio_percent) if broker_trading.volume_ratio_percent is not None else None,
        buy_brokers=[
            _broker_trading_row_response(row)
            for row in rows
            if row.side == "BUY"
        ][:5],
        sell_brokers=[
            _broker_trading_row_response(row)
            for row in rows
            if row.side == "SELL"
        ][:5],
        source=broker_trading.source,
        fetched_at=broker_trading.fetched_at,
    )


def _broker_trading_row_response(row: StockBrokerTradingRow) -> BrokerTradingRowResponse:
    return BrokerTradingRowResponse(
        rank=row.rank,
        broker_name=row.broker_name,
        buy_volume=row.buy_volume,
        sell_volume=row.sell_volume,
        net_volume=row.net_volume,
    )


def _optional_percent(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return quantize_money(numerator / denominator * Decimal("100"))


def _margin(value: Decimal | None, revenue: Decimal | None) -> Decimal | None:
    return _optional_percent(value, revenue)


def _fundamental_response(stock: Stock, session: Session) -> FundamentalResponse | None:
    if stock.asset_type == "ETF":
        return None

    quarters = list(reversed(session.scalars(
        select(StockFinancialQuarter)
        .where(StockFinancialQuarter.stock_id == stock.id)
        .order_by(StockFinancialQuarter.quarter_date.desc())
        .limit(8)
    ).all()))
    revenues = list(reversed(session.scalars(
        select(StockMonthlyRevenue)
        .where(StockMonthlyRevenue.stock_id == stock.id)
        .order_by(StockMonthlyRevenue.month_date.desc())
        .limit(3)
    ).all()))

    latest_quarter = quarters[-1] if quarters else None
    previous_quarter = quarters[-2] if len(quarters) >= 2 else None
    eps_yoy = _optional_percent(latest_quarter.eps - quarters[-5].eps, quarters[-5].eps) if len(quarters) >= 5 else None
    ttm_eps_yoy = None
    if len(quarters) >= 8:
        latest_ttm = sum((row.eps for row in quarters[-4:]), Decimal("0"))
        previous_ttm = sum((row.eps for row in quarters[-8:-4]), Decimal("0"))
        ttm_eps_yoy = _optional_percent(latest_ttm - previous_ttm, previous_ttm)

    latest_revenue = revenues[-1] if revenues else None
    revenue_yoy_values = [row.yoy_percent for row in revenues if row.yoy_percent is not None]
    three_month_yoy = (
        sum(revenue_yoy_values, Decimal("0")) / Decimal(len(revenue_yoy_values))
        if revenue_yoy_values
        else None
    )

    gross_margin = _margin(latest_quarter.gross_profit, latest_quarter.revenue) if latest_quarter else None
    previous_gross_margin = _margin(previous_quarter.gross_profit, previous_quarter.revenue) if previous_quarter else None
    operating_margin = _margin(latest_quarter.operating_income, latest_quarter.revenue) if latest_quarter else None
    previous_operating_margin = _margin(previous_quarter.operating_income, previous_quarter.revenue) if previous_quarter else None
    net_margin = _margin(latest_quarter.net_income, latest_quarter.revenue) if latest_quarter else None
    previous_net_margin = _margin(previous_quarter.net_income, previous_quarter.revenue) if previous_quarter else None

    fetched_values = [
        value
        for value in [
            latest_quarter.fetched_at if latest_quarter else None,
            latest_revenue.fetched_at if latest_revenue else None,
        ]
        if value is not None
    ]
    source_values = [
        value
        for value in [
            latest_quarter.source if latest_quarter else None,
            latest_revenue.source if latest_revenue else None,
        ]
        if value
    ]

    return FundamentalResponse(
        latest_quarter_eps=_optional_float(latest_quarter.eps if latest_quarter else None),
        eps_yoy_percent=_optional_float(eps_yoy),
        ttm_eps_yoy_percent=_optional_float(ttm_eps_yoy),
        latest_revenue_yoy_percent=_optional_float(latest_revenue.yoy_percent if latest_revenue else None),
        latest_revenue_mom_percent=_optional_float(latest_revenue.mom_percent if latest_revenue else None),
        three_month_revenue_yoy_percent=_optional_float(three_month_yoy),
        gross_margin=_optional_float(gross_margin),
        gross_margin_sos=_optional_float(gross_margin - previous_gross_margin if gross_margin is not None and previous_gross_margin is not None else None),
        operating_margin=_optional_float(operating_margin),
        operating_margin_sos=_optional_float(operating_margin - previous_operating_margin if operating_margin is not None and previous_operating_margin is not None else None),
        net_margin=_optional_float(net_margin),
        net_margin_sos=_optional_float(net_margin - previous_net_margin if net_margin is not None and previous_net_margin is not None else None),
        source=" + ".join(dict.fromkeys(source_values)) if source_values else None,
        fetched_at=max(fetched_values) if fetched_values else None,
    )


def _fundamental_trends_response(stock: Stock, session: Session) -> FundamentalTrendsResponse:
    quarters = session.scalars(
        select(StockFinancialQuarter)
        .where(StockFinancialQuarter.stock_id == stock.id)
        .order_by(StockFinancialQuarter.quarter_date.asc())
    ).all()
    revenues = session.scalars(
        select(StockMonthlyRevenue)
        .where(StockMonthlyRevenue.stock_id == stock.id)
        .order_by(StockMonthlyRevenue.month_date.asc())
    ).all()
    return FundamentalTrendsResponse(
        symbol=stock.symbol,
        categories=_fundamental_trend_categories(list(quarters), list(revenues)),
    )


def _fundamental_trend_categories(
    quarters: list[StockFinancialQuarter],
    revenues: list[StockMonthlyRevenue],
) -> list[FundamentalTrendCategoryResponse]:
    categories: list[FundamentalTrendCategoryResponse] = []
    quarter_points = _latest_year_quarters(quarters)
    revenue_points = _latest_year_revenues(revenues)
    quarter_by_date = {row.quarter_date: row for row in quarters}
    quarter_index = {row.quarter_date: index for index, row in enumerate(quarters)}

    latest_quarter = quarters[-1] if quarters else None
    latest_quarter_index = len(quarters) - 1 if quarters else None
    latest_revenue = revenues[-1] if revenues else None
    latest_revenue_yoy_values = [row.yoy_percent for row in revenues[-3:] if row.yoy_percent is not None]
    three_month_yoy = _average_decimals(latest_revenue_yoy_values)

    eps_yoy = _quarter_eps_yoy(latest_quarter, quarter_by_date) if latest_quarter else None
    ttm_eps_yoy = _ttm_eps_yoy(quarters, latest_quarter_index) if latest_quarter_index is not None else None
    categories.append(
        FundamentalTrendCategoryResponse(
            key="eps",
            label="EPS",
            unit="元",
            summary=[
                _fundamental_summary("latest_quarter_eps", "最新單季EPS", latest_quarter.eps if latest_quarter else None, "number"),
                _fundamental_summary("eps_yoy_percent", "單季EPS YoY", eps_yoy, "percent"),
                _fundamental_summary("ttm_eps_yoy_percent", "TTM EPS YoY", ttm_eps_yoy, "percent"),
            ],
            points=[
                FundamentalTrendPointResponse(
                    period=_quarter_label_from_date(row.quarter_date),
                    date=row.quarter_date,
                    value=_optional_float(row.eps),
                    yoy_percent=_optional_float(_quarter_eps_yoy(row, quarter_by_date)),
                    ttm_eps_yoy_percent=_optional_float(_ttm_eps_yoy(quarters, quarter_index[row.quarter_date])),
                )
                for row in quarter_points
            ],
            source=_fundamental_source(quarter_points),
            fetched_at=_fundamental_fetched_at(quarter_points),
        )
    )

    categories.append(
        FundamentalTrendCategoryResponse(
            key="monthly_revenue",
            label="月營收",
            unit="元",
            summary=[
                _fundamental_summary(
                    "latest_revenue_yoy_percent",
                    "最新月營收YoY",
                    latest_revenue.yoy_percent if latest_revenue else None,
                    "percent",
                ),
                _fundamental_summary(
                    "latest_revenue_mom_percent",
                    "最新月營收MoM",
                    latest_revenue.mom_percent if latest_revenue else None,
                    "percent",
                ),
                _fundamental_summary("three_month_revenue_yoy_percent", "近三月營收YoY", three_month_yoy, "percent"),
            ],
            points=[
                FundamentalTrendPointResponse(
                    period=_month_label_from_date(row.month_date),
                    date=row.month_date,
                    value=_optional_float(row.revenue),
                    yoy_percent=_optional_float(row.yoy_percent),
                    mom_percent=_optional_float(row.mom_percent),
                )
                for row in revenue_points
            ],
            source=_fundamental_source(revenue_points),
            fetched_at=_fundamental_fetched_at(revenue_points),
        )
    )

    margin_configs = [
        ("gross_margin", "毛利率", "gross_profit"),
        ("operating_margin", "營益率", "operating_income"),
        ("net_margin", "淨利率", "net_income"),
    ]
    for key, label, field_name in margin_configs:
        latest_margin = _quarter_margin(latest_quarter, field_name) if latest_quarter else None
        latest_sos = _quarter_margin_sos(latest_quarter, field_name, quarter_by_date) if latest_quarter else None
        categories.append(
            FundamentalTrendCategoryResponse(
                key=key,
                label=label,
                unit="%",
                summary=[
                    _fundamental_summary(key, label, latest_margin, "percent"),
                    _fundamental_summary(f"{key}_sos", f"{label}SoS", latest_sos, "percent"),
                ],
                points=[
                    FundamentalTrendPointResponse(
                        period=_quarter_label_from_date(row.quarter_date),
                        date=row.quarter_date,
                        value=_optional_float(_quarter_margin(row, field_name)),
                        sos_percent=_optional_float(_quarter_margin_sos(row, field_name, quarter_by_date)),
                    )
                    for row in quarter_points
                ],
                source=_fundamental_source(quarter_points),
                fetched_at=_fundamental_fetched_at(quarter_points),
            )
        )

    return categories


def _latest_year_quarters(rows: list[StockFinancialQuarter]) -> list[StockFinancialQuarter]:
    if not rows:
        return []
    latest = rows[-1].quarter_date
    start = date(latest.year - 1, latest.month, latest.day)
    return [row for row in rows if row.quarter_date >= start]


def _latest_year_revenues(rows: list[StockMonthlyRevenue]) -> list[StockMonthlyRevenue]:
    if not rows:
        return []
    latest = rows[-1].month_date
    start = date(latest.year - 1, latest.month, 1)
    return [row for row in rows if row.month_date >= start]


def _quarter_eps_yoy(
    row: StockFinancialQuarter | None,
    rows_by_date: dict[date, StockFinancialQuarter],
) -> Decimal | None:
    if row is None:
        return None
    prior = rows_by_date.get(date(row.quarter_date.year - 1, row.quarter_date.month, row.quarter_date.day))
    return _optional_percent(row.eps - prior.eps, prior.eps) if prior else None


def _ttm_eps_yoy(rows: list[StockFinancialQuarter], index: int | None) -> Decimal | None:
    if index is None or index < 7:
        return None
    latest_ttm = sum((row.eps for row in rows[index - 3 : index + 1]), Decimal("0"))
    previous_ttm = sum((row.eps for row in rows[index - 7 : index - 3]), Decimal("0"))
    return _optional_percent(latest_ttm - previous_ttm, previous_ttm)


def _quarter_margin(row: StockFinancialQuarter | None, field_name: str) -> Decimal | None:
    return _margin(getattr(row, field_name), row.revenue) if row else None


def _quarter_margin_sos(
    row: StockFinancialQuarter | None,
    field_name: str,
    rows_by_date: dict[date, StockFinancialQuarter],
) -> Decimal | None:
    if row is None:
        return None
    current_margin = _quarter_margin(row, field_name)
    previous = rows_by_date.get(_previous_quarter_date(row.quarter_date))
    previous_margin = _quarter_margin(previous, field_name)
    if current_margin is None or previous_margin is None:
        return None
    return quantize_money(current_margin - previous_margin)


def _previous_quarter_date(value: date) -> date:
    if value.month == 3:
        return date(value.year - 1, 12, 31)
    if value.month == 6:
        return date(value.year, 3, 31)
    if value.month == 9:
        return date(value.year, 6, 30)
    return date(value.year, 9, 30)


def _quarter_label_from_date(value: date) -> str:
    return f"{value.year}Q{((value.month - 1) // 3) + 1}"


def _month_label_from_date(value: date) -> str:
    return f"{value.year}/{value.month:02d}"


def _average_decimals(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return quantize_money(sum(values, Decimal("0")) / Decimal(len(values)))


def _fundamental_summary(
    key: str,
    label: str,
    value: Decimal | None,
    value_type: str,
) -> FundamentalTrendSummaryResponse:
    return FundamentalTrendSummaryResponse(
        key=key,
        label=label,
        value=_optional_float(value),
        value_type=value_type,
    )


def _fundamental_source(rows) -> str | None:
    return " + ".join(dict.fromkeys(row.source for row in rows if row.source)) or None


def _fundamental_fetched_at(rows) -> datetime | None:
    values = [row.fetched_at for row in rows if row.fetched_at is not None]
    return max(values) if values else None


def _stock_response(stock: Stock, session: Session) -> StockResponse:
    metric = _latest_metric(session, stock.id)
    position = session.scalar(select(StockPosition).where(StockPosition.stock_id == stock.id))
    broker = _selected_broker(session)
    valuations = []
    if stock.asset_type != "ETF":
        valuations = session.scalars(
            select(StockValuation)
            .where(
                StockValuation.stock_id == stock.id,
                StockValuation.current_pe > 0,
                StockValuation.eps_value > 0,
            )
            .order_by(StockValuation.eps_type.desc())
        ).all()

    return StockResponse(
        symbol=stock.symbol,
        name=stock.name,
        asset_type=stock.asset_type,
        market=stock.market,
        currency=stock.currency,
        is_active=stock.is_active,
        display_order=stock.display_order,
        metric=StockMetricResponse(
            open_price=_optional_float(metric.open_price),
            previous_close=_optional_float(metric.previous_close),
            day_high=_optional_float(metric.day_high),
            day_low=_optional_float(metric.day_low),
            current_price=_float(metric.current_price),
            change_percent=_optional_float(metric.change_percent),
            current_pe=_optional_positive_float(metric.current_pe),
            pe_average_3y=_optional_positive_float(metric.pe_average_3y),
            pe_min_3y=_optional_positive_float(metric.pe_min_3y),
            pe_max_3y=_optional_positive_float(metric.pe_max_3y),
            pe_vs_average_percent=_optional_float(metric.pe_vs_average_percent),
            price_updated_at=metric.price_updated_at,
            pe_updated_at=metric.pe_updated_at,
            pe_data_date=metric.pe_data_date,
            source=metric.source,
        )
        if metric
        else None,
        position=_position_response(position, metric, broker, stock.asset_type),
        broker_trading=_broker_trading_response(stock, session),
        fundamental=_fundamental_response(stock, session),
        valuations=[_valuation_response(valuation, position.buy_price if position else None) for valuation in valuations],
    )


def _technical_analysis_response(stock: Stock, session: Session, limit: int) -> TechnicalAnalysisResponse:
    ma_lookback = max(MOVING_AVERAGE_PERIODS) - 1
    rows = session.scalars(
        select(StockDailyPrice)
        .where(StockDailyPrice.stock_id == stock.id)
        .order_by(StockDailyPrice.trade_date.desc())
        .limit(limit + ma_lookback)
    ).all()
    candles = [
        {
            "date": row.trade_date,
            "open": row.open_price,
            "high": row.high_price,
            "low": row.low_price,
            "close": row.close_price,
            "volume": row.volume,
            "source": row.source,
            "fetched_at": row.fetched_at,
            "is_provisional": False,
        }
        for row in reversed(rows)
    ]

    metric = _latest_metric(session, stock.id)
    now = datetime.now(TAIPEI_TZ)
    if metric:
        metric_time = _as_taipei(metric.price_updated_at)
        latest_historical_date = candles[-1]["date"] if candles else None
        should_merge_provisional = (
            latest_historical_date is None
            or metric_time.date() > latest_historical_date
            or (_market_is_open(now) and metric_time.date() == now.date())
        )
        if should_merge_provisional:
            current_price = metric.current_price
            open_price = metric.open_price or current_price
            provisional = {
                "date": metric_time.date(),
                "open": open_price,
                "high": metric.day_high or max(open_price, current_price),
                "low": metric.day_low or min(open_price, current_price),
                "close": current_price,
                "volume": None,
                "source": f"{metric.source} provisional quote",
                "fetched_at": metric.price_updated_at,
                "is_provisional": True,
            }
            candles = [candle for candle in candles if candle["date"] != provisional["date"]]
            candles.append(provisional)

    rolling_closes: list[Decimal] = []
    rolling_volumes: list[Decimal] = []
    response_candles = []
    for candle in candles:
        rolling_closes.append(candle["close"])
        if len(rolling_closes) > max(MOVING_AVERAGE_PERIODS):
            rolling_closes.pop(0)
        ma_values = moving_averages(rolling_closes)
        volume_lots = _volume_lots(candle["volume"])
        if volume_lots is not None:
            rolling_volumes.append(volume_lots)
            if len(rolling_volumes) > 20:
                rolling_volumes.pop(0)
            volume_ma_values = moving_averages(rolling_volumes, periods=(5, 20))
        else:
            volume_ma_values = {5: None, 20: None}
        volume_ma20 = volume_ma_values[20]
        response_candles.append(
            TechnicalCandleResponse(
                date=candle["date"],
                open=_float(candle["open"]),
                high=_float(candle["high"]),
                low=_float(candle["low"]),
                close=_float(candle["close"]),
                volume=_optional_float(volume_lots),
                volume_ma5=_optional_float(volume_ma_values[5]),
                volume_ma20=_optional_float(volume_ma20),
                volume_vs_ma20_percent=_optional_float(_volume_vs_ma20_percent(volume_lots, volume_ma20)),
                ma5=_optional_float(ma_values[5]),
                ma10=_optional_float(ma_values[10]),
                ma20=_optional_float(ma_values[20]),
                ma60=_optional_float(ma_values[60]),
                ma120=_optional_float(ma_values[120]),
                ma240=_optional_float(ma_values[240]),
                is_provisional=candle["is_provisional"],
            )
        )

    visible_candles = response_candles[-limit:]
    visible_dates = {candle.date for candle in visible_candles}
    visible_raw = [candle for candle in candles if candle["date"] in visible_dates]
    fetched_at = max((candle["fetched_at"] for candle in visible_raw), default=None)
    sources = list(dict.fromkeys(candle["source"] for candle in visible_raw))
    return TechnicalAnalysisResponse(
        symbol=stock.symbol,
        interval="1d",
        source=" + ".join(sources) if sources else "FinMind TaiwanStockPrice",
        fetched_at=fetched_at,
        candles=visible_candles,
    )


def _schema_dump(value):
    return value.model_dump(mode="json") if value is not None else None


def _ratio_percent(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return round((numerator - denominator) / denominator * 100, 2)


def _technical_summary_for_ai(stock: Stock, session: Session) -> dict:
    analysis = _technical_analysis_response(stock, session, 120)
    latest = analysis.candles[-1] if analysis.candles else None
    if not latest:
        return {
            "interval": "1d",
            "source": analysis.source,
            "fetched_at": analysis.fetched_at.isoformat() if analysis.fetched_at else None,
            "latest": None,
        }

    ma_values = {
        "ma5": latest.ma5,
        "ma10": latest.ma10,
        "ma20": latest.ma20,
        "ma60": latest.ma60,
        "ma120": latest.ma120,
        "ma240": latest.ma240,
    }
    return {
        "interval": analysis.interval,
        "source": analysis.source,
        "fetched_at": analysis.fetched_at.isoformat() if analysis.fetched_at else None,
        "latest": {
            "date": latest.date.isoformat(),
            "close_price_twd": latest.close,
            "is_provisional": latest.is_provisional,
            **ma_values,
            "price_vs_ma20_percent": _ratio_percent(latest.close, latest.ma20),
            "today_volume_lots": latest.volume,
            "volume_ma5_lots": latest.volume_ma5,
            "volume_ma20_lots": latest.volume_ma20,
            "volume_as_percent_of_ma20": latest.volume_vs_ma20_percent,
            "volume_difference_vs_ma20_percent": (
                round(latest.volume_vs_ma20_percent - 100, 2)
                if latest.volume_vs_ma20_percent is not None
                else None
            ),
        },
    }


def _ai_stock_summary(stock: Stock, session: Session, analysis_mode: str) -> dict:
    stock_response = _stock_response(stock, session)
    broker_trading = stock_response.broker_trading
    metric = stock_response.metric
    summary = {
        "symbol": stock_response.symbol,
        "name": stock_response.name,
        "asset_type": stock_response.asset_type,
        "market": stock_response.market,
        "currency": stock_response.currency,
        "summary_version": 2,
        "prompt_version": PROMPT_VERSION,
        "analysis_mode": analysis_mode,
        "quote": None
        if metric is None
        else {
            "current_price_twd": metric.current_price,
            "open_price_twd": metric.open_price,
            "previous_close_twd": metric.previous_close,
            "day_high_twd": metric.day_high,
            "day_low_twd": metric.day_low,
            "price_updated_at": metric.price_updated_at.isoformat(),
            "source": metric.source,
        },
        "pe_context": None
        if metric is None
        else {
            "current_pe": metric.current_pe,
            "pe_average_3y": metric.pe_average_3y,
            "pe_min_3y": metric.pe_min_3y,
            "pe_max_3y": metric.pe_max_3y,
            "current_pe_vs_average_percent": metric.pe_vs_average_percent,
            "pe_updated_at": metric.pe_updated_at.isoformat(),
            "pe_data_date": (
                getattr(metric, "pe_data_date", None).isoformat()
                if getattr(metric, "pe_data_date", None)
                else None
            ),
        },
        "valuation_scenarios": [
            _ai_valuation_summary(valuation, analysis_mode)
            for valuation in stock_response.valuations
        ],
        "fundamental": _schema_dump(stock_response.fundamental),
        "technical": _technical_summary_for_ai(stock, session),
        "chip": None
        if broker_trading is None
        else {
            "trade_date": broker_trading.trade_date,
            "main_net_volume_lots": broker_trading.main_net_volume,
            "main_buy_volume_lots": abs(broker_trading.main_buy_volume),
            "main_sell_volume_lots": abs(broker_trading.main_sell_volume),
            "volume_ratio_percent": broker_trading.volume_ratio_percent,
            "source": broker_trading.source,
            "fetched_at": broker_trading.fetched_at.isoformat(),
        },
    }
    if analysis_mode == AI_MODE_HELD:
        position = stock_response.position
        if position is None:
            raise ValueError("HELD analysis requires a stock position.")
        summary["position"] = {
            "average_cost_price_twd": position.buy_price,
            "unrealized_profit_loss_per_share_twd": position.unrealized_profit_loss,
            "unrealized_return_percent": position.unrealized_profit_loss_percent,
            "fee_adjusted_profit_loss_per_share_twd": position.fee_adjusted_profit_loss,
            "fee_adjusted_return_percent": position.fee_adjusted_profit_loss_percent,
            "broker_id": position.broker_id,
            "broker_fee_rate": position.broker_fee_rate,
        }
    return summary


def _ai_valuation_summary(valuation: StockValuationResponse, analysis_mode: str) -> dict:
    result = {
        "eps_type": valuation.eps_type,
        "eps_value": valuation.eps_value,
        "eps_period": valuation.eps_period,
        "mechanical_eps_times_current_pe_price_twd": valuation.estimated_price,
        "mechanical_price_vs_current_price_percent": valuation.difference_percent,
        "scenario_label": valuation.valuation_status,
        "calculated_at": valuation.calculated_at.isoformat(),
        "method_note": "EPS multiplied by current PE; this is a mechanical scenario, not a forecast or fair-value guarantee.",
    }
    if analysis_mode == AI_MODE_HELD:
        result.update(
            {
                "mechanical_price_vs_average_cost_twd": valuation.cost_difference,
                "mechanical_price_vs_average_cost_percent": valuation.cost_difference_percent,
            }
        )
    return result


def _ai_analysis_result_response(row: StockAIAnalysis, cached: bool) -> StockAIAnalysisResultResponse:
    try:
        analysis_payload = json.loads(row.response_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="Cached AI analysis is invalid.") from exc
    return StockAIAnalysisResultResponse(
        mode=row.analysis_mode,
        provider=row.provider,
        model=row.model,
        prompt_version=row.prompt_version,
        cached=cached,
        analysis_date=row.analysis_date,
        generated_at=row.updated_at,
        analysis=normalize_ai_analysis(analysis_payload, row.analysis_mode),
    )


def _ai_analysis_is_cacheable(row: StockAIAnalysis | None) -> bool:
    if row is None or row.status != "success":
        return False
    try:
        payload = json.loads(row.response_json)
    except json.JSONDecodeError:
        return False
    if isinstance(payload, dict) and isinstance(payload.get("analysis"), dict):
        payload = payload["analysis"]
    if not isinstance(payload, dict):
        return False
    if payload.get("format_valid", True) is not True:
        return False
    risk_points = payload.get("risk_points") or []
    if any("格式不是 JSON" in str(point) for point in risk_points):
        return False
    if "格式不是 JSON" in str(payload.get("summary") or ""):
        return False
    try:
        validation_errors = json.loads(getattr(row, "validation_errors_json", None) or "[]")
    except json.JSONDecodeError:
        return False
    if any("warning:" not in str(error) for error in validation_errors):
        return False
    return True


def _ai_analysis_batch_response(
    symbol: str,
    results: dict[str, tuple[StockAIAnalysis, bool]],
    errors: dict[str, str] | None = None,
    running: dict[str, bool] | None = None,
) -> StockAIAnalysisResponse:
    return StockAIAnalysisResponse(
        symbol=symbol,
        analyses=StockAIAnalysisModesResponse(
            unheld=(
                _ai_analysis_result_response(*results[AI_MODE_UNHELD])
                if AI_MODE_UNHELD in results
                else None
            ),
            held=(
                _ai_analysis_result_response(*results[AI_MODE_HELD])
                if AI_MODE_HELD in results
                else None
            ),
        ),
        errors=errors or {},
        running=running or {},
    )


def _current_ai_cache_row(
    session: Session,
    stock: Stock,
    provider,
    analysis_mode: str,
    stock_summary: dict,
    analysis_date: date,
) -> tuple[StockAIAnalysis | None, str]:
    input_hash = stock_summary_hash(stock_summary)
    row = session.scalar(
        select(StockAIAnalysis)
        .where(
            StockAIAnalysis.stock_id == stock.id,
            StockAIAnalysis.provider == provider.provider_id,
            StockAIAnalysis.model == provider.model,
            StockAIAnalysis.analysis_mode == analysis_mode,
            StockAIAnalysis.prompt_version == PROMPT_VERSION,
            StockAIAnalysis.analysis_date == analysis_date,
            StockAIAnalysis.input_hash == input_hash,
        )
        .order_by(StockAIAnalysis.updated_at.desc())
        .limit(1)
    )
    return row, input_hash


def _latest_ai_cache_row(
    session: Session,
    stock: Stock,
    provider_id: str,
    model: str,
    analysis_mode: str,
) -> StockAIAnalysis | None:
    candidates = session.scalars(
        select(StockAIAnalysis)
        .where(
            StockAIAnalysis.stock_id == stock.id,
            StockAIAnalysis.provider == provider_id,
            StockAIAnalysis.model == model,
            StockAIAnalysis.analysis_mode == analysis_mode,
            StockAIAnalysis.prompt_version == PROMPT_VERSION,
            StockAIAnalysis.status.in_(("success", "format_fallback")),
        )
        .order_by(StockAIAnalysis.updated_at.desc())
        .limit(20)
    ).all()
    for row in candidates:
        if _ai_analysis_is_cacheable(row):
            return row
        if _repair_ai_analysis_cache_row(session, row):
            return row
    return None


def _repair_ai_analysis_cache_row(session: Session, row: StockAIAnalysis) -> bool:
    if row.status != "format_fallback" or not row.raw_response_text:
        return False
    analysis, validation_errors = normalize_ai_analysis_with_errors(
        row.raw_response_text,
        row.analysis_mode,
    )
    if not analysis.format_valid:
        return False
    row.response_json = json.dumps(analysis.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    row.validation_errors_json = json.dumps(validation_errors, ensure_ascii=False)
    row.status = "success"
    row.error_message = None
    session.commit()
    session.refresh(row)
    return True


def _repairable_current_ai_cache_row(
    session: Session,
    stock: Stock,
    provider,
    analysis_mode: str,
    analysis_date: date,
    input_hash: str,
) -> StockAIAnalysis | None:
    candidates = session.scalars(
        select(StockAIAnalysis)
        .where(
            StockAIAnalysis.stock_id == stock.id,
            StockAIAnalysis.provider == provider.provider_id,
            StockAIAnalysis.model == provider.model,
            StockAIAnalysis.analysis_mode == analysis_mode,
            StockAIAnalysis.prompt_version == PROMPT_VERSION,
            StockAIAnalysis.analysis_date == analysis_date,
            StockAIAnalysis.input_hash == input_hash,
            StockAIAnalysis.status == "format_fallback",
        )
        .order_by(StockAIAnalysis.updated_at.desc())
        .limit(20)
    ).all()
    return next((candidate for candidate in candidates if _repair_ai_analysis_cache_row(session, candidate)), None)


def _ai_analysis_is_fresh_inflight(row: StockAIAnalysis) -> bool:
    if row.status not in {"queued", "running"}:
        return False
    updated_at = row.updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - updated_at.astimezone(UTC) <= AI_ANALYSIS_INFLIGHT_TIMEOUT


def _latest_ai_inflight_row(
    session: Session,
    stock: Stock,
    provider_id: str,
    model: str,
    analysis_mode: str,
) -> StockAIAnalysis | None:
    candidates = session.scalars(
        select(StockAIAnalysis)
        .where(
            StockAIAnalysis.stock_id == stock.id,
            StockAIAnalysis.provider == provider_id,
            StockAIAnalysis.model == model,
            StockAIAnalysis.analysis_mode == analysis_mode,
            StockAIAnalysis.prompt_version == PROMPT_VERSION,
            StockAIAnalysis.status.in_(("queued", "running")),
        )
        .order_by(StockAIAnalysis.updated_at.desc())
        .limit(5)
    ).all()
    return next((candidate for candidate in candidates if _ai_analysis_is_fresh_inflight(candidate)), None)


def _current_ai_inflight_row(
    session: Session,
    stock: Stock,
    provider,
    analysis_mode: str,
    analysis_date: date,
    input_hash: str,
) -> StockAIAnalysis | None:
    candidates = session.scalars(
        select(StockAIAnalysis)
        .where(
            StockAIAnalysis.stock_id == stock.id,
            StockAIAnalysis.provider == provider.provider_id,
            StockAIAnalysis.model == provider.model,
            StockAIAnalysis.analysis_mode == analysis_mode,
            StockAIAnalysis.prompt_version == PROMPT_VERSION,
            StockAIAnalysis.analysis_date == analysis_date,
            StockAIAnalysis.input_hash == input_hash,
            StockAIAnalysis.status.in_(("queued", "running")),
        )
        .order_by(StockAIAnalysis.updated_at.desc())
        .limit(5)
    ).all()
    return next((candidate for candidate in candidates if _ai_analysis_is_fresh_inflight(candidate)), None)


def _generate_ai_mode(
    session: Session,
    stock: Stock,
    provider,
    analysis_mode: str,
    force_refresh: bool,
) -> tuple[StockAIAnalysis | None, bool, str | None, bool]:
    stock_summary = _ai_stock_summary(stock, session, analysis_mode)
    analysis_date = datetime.now(TAIPEI_TZ).date()
    row, input_hash = _current_ai_cache_row(
        session,
        stock,
        provider,
        analysis_mode,
        stock_summary,
        analysis_date,
    )
    inflight_row = _current_ai_inflight_row(session, stock, provider, analysis_mode, analysis_date, input_hash)
    if inflight_row is not None:
        return None, False, "AI 分析正在處理中，完成後會自動讀取快取。", True
    if row and _ai_analysis_is_cacheable(row) and not force_refresh:
        return row, True, None, False
    if not force_refresh:
        repaired_row = _repairable_current_ai_cache_row(
            session,
            stock,
            provider,
            analysis_mode,
            analysis_date,
            input_hash,
        )
        if repaired_row is not None:
            return repaired_row, True, None, False
    row = None

    request_payload_json = json.dumps(stock_summary, ensure_ascii=False, sort_keys=True)
    if row is None:
        row = StockAIAnalysis(
            stock_id=stock.id,
            provider=provider.provider_id,
            model=provider.model,
            analysis_mode=analysis_mode,
            prompt_version=PROMPT_VERSION,
            analysis_date=analysis_date,
            input_hash=input_hash,
            request_payload_json=request_payload_json,
            response_json="{}",
            status="running",
        )
        session.add(row)
        session.commit()
        session.refresh(row)

    try:
        provider_result = provider.analyze_stock(stock_summary, analysis_mode)
    except AIAnalysisError as exc:
        now = datetime.now(UTC)
        row.request_payload_json = request_payload_json
        row.response_json = "{}"
        row.raw_response_text = None
        row.provider_metadata_json = None
        row.validation_errors_json = json.dumps([], ensure_ascii=False)
        row.status = "failed"
        row.error_message = str(exc)
        row.updated_at = now
        session.commit()
        return None, False, str(exc), False

    now = datetime.now(UTC)
    row.request_payload_json = request_payload_json
    row.response_json = json.dumps(provider_result.analysis.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    row.raw_response_text = provider_result.raw_response_text
    row.provider_metadata_json = json.dumps(provider_result.provider_metadata, ensure_ascii=False, sort_keys=True)
    row.validation_errors_json = json.dumps(provider_result.validation_errors, ensure_ascii=False)
    row.status = "success" if provider_result.analysis.format_valid else "format_fallback"
    row.error_message = None if provider_result.analysis.format_valid else "AI response failed validation."
    row.updated_at = now
    session.commit()
    session.refresh(row)
    if not provider_result.analysis.format_valid:
        return None, False, "AI 回覆未通過格式或內容驗證，已保留 Log 供後續檢查。", False
    return row, False, None, False


def _json_field(value: str | None):
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _ai_log_record(row: StockAIAnalysis, symbol: str) -> dict:
    return {
        "id": row.id,
        "symbol": symbol,
        "analysis_mode": row.analysis_mode,
        "prompt_version": row.prompt_version,
        "provider": row.provider,
        "model": row.model,
        "analysis_date": row.analysis_date.isoformat(),
        "input_hash": row.input_hash,
        "status": row.status,
        "error_message": row.error_message,
        "request_payload": _json_field(row.request_payload_json),
        "normalized_response": _json_field(row.response_json),
        "raw_response_text": row.raw_response_text,
        "provider_metadata": _json_field(row.provider_metadata_json),
        "validation_errors": _json_field(row.validation_errors_json) or [],
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _as_taipei(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(TAIPEI_TZ)


def _market_is_open(now: datetime) -> bool:
    return now.weekday() < 5 and MARKET_OPEN_TIME <= now.time().replace(tzinfo=None) < MARKET_CLOSE_TIME


def _volume_lots(volume: int | None) -> Decimal | None:
    if volume is None:
        return None
    return (Decimal(volume) / Decimal("1000")).quantize(Decimal("0.01"))


def _volume_vs_ma20_percent(volume_lots: Decimal | None, volume_ma20: Decimal | None) -> Decimal | None:
    if volume_lots is None or volume_ma20 is None or volume_ma20 == 0:
        return None
    return ((volume_lots / volume_ma20) * Decimal("100")).quantize(Decimal("0.01"))


@app.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    ping_database()
    return HealthResponse(
        status="ok",
        app_env=settings.app_env,
        database=_database_kind(),
        api_version=settings.api_version,
    )


@app.get("/api/metadata", response_model=MetadataResponse)
async def metadata(session: Session = Depends(get_session)) -> MetadataResponse:
    refresh_status = await refresh_manager.snapshot()
    return MetadataResponse(
        data_source="TWSE/FinMind quote + TWSE/FinMind latest-date PE + FinMind EPS/fundamentals/daily prices + Yahoo broker trading",
        api_version=settings.api_version,
        stocks_count=len(_active_stocks(session)),
        valuations_count=_active_valuations_count(session),
        refresh_status=refresh_status["status"],
        refresh_interval_seconds=settings.background_refresh_seconds,
        auto_refresh_enabled=refresh_status["auto_refresh_enabled"],
        market_session=refresh_status["market_session"],
        refresh_window=refresh_status["refresh_window"],
        next_auto_refresh_at=refresh_status["next_auto_refresh_at"],
        last_refresh_finished_at=refresh_status["last_refresh_finished_at"],
        last_close_verification_at=refresh_status["last_close_verification_at"],
        latest_official_data_date=_latest_official_data_date(session),
    )


@app.get("/api/settings/broker", response_model=BrokerSettingResponse)
def get_broker_setting(session: Session = Depends(get_session)) -> BrokerSettingResponse:
    return _broker_setting_response(session)


@app.get("/api/futures/wtx", response_model=FuturesWtxResponse)
def get_wtx_futures() -> FuturesWtxResponse:
    return FuturesWtxResponse(**latest_wtx_response())


@app.put("/api/settings/broker", response_model=BrokerSettingResponse)
def update_broker_setting(
    payload: BrokerSettingRequest,
    session: Session = Depends(get_session),
) -> BrokerSettingResponse:
    try:
        broker = get_broker(payload.broker_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    set_app_setting(session, "selected_broker", broker.broker_id)
    return _broker_setting_response(session)


@app.get("/api/stocks", response_model=list[StockResponse])
def list_stocks(session: Session = Depends(get_session)) -> list[StockResponse]:
    return [_stock_response(stock, session) for stock in _active_stocks(session)]


@app.post("/api/stocks/reorder", response_model=list[StockResponse])
def reorder_stocks(payload: StockReorderRequest, session: Session = Depends(get_session)) -> list[StockResponse]:
    symbols = [symbol.strip() for symbol in payload.symbols]
    if len(symbols) != len(set(symbols)):
        raise HTTPException(status_code=400, detail="Stock symbols must be unique.")

    active_stocks = _active_stocks(session)
    active_by_symbol = {stock.symbol: stock for stock in active_stocks}
    if set(symbols) != set(active_by_symbol):
        raise HTTPException(status_code=400, detail="Symbols must match all active stocks.")

    for index, symbol in enumerate(symbols, start=1):
        active_by_symbol[symbol].display_order = index * 10
        active_by_symbol[symbol].updated_at = datetime.now(UTC)

    session.commit()
    return [_stock_response(stock, session) for stock in _active_stocks(session)]


@app.get("/api/refresh/status", response_model=RefreshStatusResponse)
async def refresh_status() -> RefreshStatusResponse:
    return RefreshStatusResponse(**await refresh_manager.snapshot())


@app.post("/api/stocks/refresh", response_model=RefreshQueueResponse, status_code=status.HTTP_202_ACCEPTED)
async def refresh_all_stocks() -> RefreshQueueResponse:
    states = await refresh_manager.queue_active_stocks(force_full=True)
    queued_at = datetime.now(UTC)
    symbols = [state.symbol for state in states]
    if states:
        queued_at = min((state.queued_at for state in states if state.queued_at), default=queued_at)

    return RefreshQueueResponse(
        status="queued" if symbols else "idle",
        symbols=symbols,
        queued_at=queued_at,
        message="Active stocks queued for full data refresh." if symbols else "No active stocks to refresh.",
    )


@app.get("/api/stocks/{symbol}", response_model=StockResponse)
def get_stock(symbol: str, session: Session = Depends(get_session)) -> StockResponse:
    stock = session.scalar(select(Stock).where(Stock.symbol == symbol))
    if not stock:
        raise HTTPException(status_code=404, detail="Stock not found")

    return _stock_response(stock, session)


@app.get("/api/stocks/{symbol}/fundamentals/trends", response_model=FundamentalTrendsResponse)
def get_stock_fundamental_trends(symbol: str, session: Session = Depends(get_session)) -> FundamentalTrendsResponse:
    try:
        normalized_symbol = normalize_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    stock = session.scalar(select(Stock).where(Stock.symbol == normalized_symbol, Stock.is_active.is_(True)))
    if not stock:
        raise HTTPException(status_code=404, detail="Stock not found")
    if stock.asset_type == "ETF":
        raise HTTPException(status_code=404, detail="Fundamentals are not applicable to ETFs")

    return _fundamental_trends_response(stock, session)


@app.get("/api/stocks/{symbol}/ai-analysis/latest", response_model=StockAIAnalysisResponse)
def get_latest_stock_ai_analysis(
    symbol: str,
    provider: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> StockAIAnalysisResponse:
    try:
        normalized_symbol = normalize_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    stock = session.scalar(select(Stock).where(Stock.symbol == normalized_symbol, Stock.is_active.is_(True)))
    if not stock:
        raise HTTPException(status_code=404, detail="Stock not found")
    try:
        provider_id, model = ai_provider_identity(settings, provider)
    except AIConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    modes = [AI_MODE_UNHELD]
    if session.scalar(select(StockPosition).where(StockPosition.stock_id == stock.id)):
        modes.append(AI_MODE_HELD)
    results = {
        mode: (row, True)
        for mode in modes
        if (row := _latest_ai_cache_row(session, stock, provider_id, model, mode)) is not None
    }
    running = {
        mode.lower(): True
        for mode in modes
        if _latest_ai_inflight_row(session, stock, provider_id, model, mode) is not None
    }
    return _ai_analysis_batch_response(stock.symbol, results, running=running)


@app.post("/api/stocks/{symbol}/ai-analysis", response_model=StockAIAnalysisResponse)
def create_stock_ai_analysis(
    symbol: str,
    response: Response,
    payload: StockAIAnalysisRequest = Body(default_factory=StockAIAnalysisRequest),
    session: Session = Depends(get_session),
) -> StockAIAnalysisResponse:
    try:
        normalized_symbol = normalize_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    stock = session.scalar(select(Stock).where(Stock.symbol == normalized_symbol, Stock.is_active.is_(True)))
    if not stock:
        raise HTTPException(status_code=404, detail="Stock not found")
    try:
        ai_provider = build_ai_provider(settings, payload.provider)
    except AIConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    modes = [AI_MODE_UNHELD]
    if session.scalar(select(StockPosition).where(StockPosition.stock_id == stock.id)):
        modes.append(AI_MODE_HELD)

    results: dict[str, tuple[StockAIAnalysis, bool]] = {}
    errors: dict[str, str] = {}
    running: dict[str, bool] = {}
    for mode in modes:
        row, cached, error, is_running = _generate_ai_mode(
            session,
            stock,
            ai_provider,
            mode,
            payload.force_refresh,
        )
        key = mode.lower()
        if row is not None:
            results[mode] = (row, cached)
        elif is_running:
            running[key] = True
        elif error:
            errors[key] = error

    if not results:
        if running:
            response.status_code = status.HTTP_202_ACCEPTED
            return _ai_analysis_batch_response(stock.symbol, results, errors, running)
        details = "；".join(f"{mode}: {message}" for mode, message in errors.items())
        raise HTTPException(status_code=502, detail=details or "AI analysis failed.")
    if running:
        response.status_code = status.HTTP_202_ACCEPTED
    return _ai_analysis_batch_response(stock.symbol, results, errors, running)


@app.get("/api/ai-analysis/logs/export")
def export_ai_analysis_logs(
    format: str = Query(default="json", pattern="^(json|csv)$"),
    symbol: str | None = Query(default=None),
    mode: str | None = Query(default=None),
    provider: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=5000),
    session: Session = Depends(get_session),
):
    query = (
        select(StockAIAnalysis, Stock.symbol)
        .join(Stock, Stock.id == StockAIAnalysis.stock_id)
        .order_by(StockAIAnalysis.updated_at.desc())
        .limit(limit)
    )
    if symbol:
        try:
            query = query.where(Stock.symbol == normalize_symbol(symbol))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if mode:
        normalized_mode = mode.strip().upper()
        if normalized_mode not in {"GENERAL", AI_MODE_UNHELD, AI_MODE_HELD}:
            raise HTTPException(status_code=400, detail="Unsupported analysis mode.")
        query = query.where(StockAIAnalysis.analysis_mode == normalized_mode)
    if provider:
        query = query.where(StockAIAnalysis.provider == provider.strip().lower())
    if date_from:
        query = query.where(StockAIAnalysis.analysis_date >= date_from)
    if date_to:
        query = query.where(StockAIAnalysis.analysis_date <= date_to)

    records = [_ai_log_record(row, stock_symbol) for row, stock_symbol in session.execute(query).all()]
    filename = f"ai-analysis-logs-{datetime.now(TAIPEI_TZ).date().isoformat()}"
    if format == "json":
        return JSONResponse(
            content=jsonable_encoder(records),
            headers={"Content-Disposition": f'attachment; filename="{filename}.json"'},
        )

    output = io.StringIO()
    fieldnames = list(records[0].keys()) if records else [
        "id",
        "symbol",
        "analysis_mode",
        "prompt_version",
        "provider",
        "model",
        "analysis_date",
        "input_hash",
        "status",
        "error_message",
        "request_payload",
        "normalized_response",
        "raw_response_text",
        "provider_metadata",
        "validation_errors",
        "created_at",
        "updated_at",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for record in records:
        writer.writerow(
            {
                key: json.dumps(value, ensure_ascii=False, sort_keys=True)
                if isinstance(value, (dict, list))
                else value
                for key, value in record.items()
            }
        )
    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'},
    )


@app.get("/api/stocks/{symbol}/technical-analysis", response_model=TechnicalAnalysisResponse)
def get_technical_analysis(
    symbol: str,
    limit: int = Query(default=120, ge=20, le=250),
    session: Session = Depends(get_session),
) -> TechnicalAnalysisResponse:
    try:
        normalized_symbol = normalize_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    stock = session.scalar(
        select(Stock).where(Stock.symbol == normalized_symbol, Stock.is_active.is_(True))
    )
    if not stock:
        raise HTTPException(status_code=404, detail="Stock not found")
    return _technical_analysis_response(stock, session, limit)


@app.delete("/api/stocks/{symbol}", response_model=StockDeleteResponse)
async def delete_stock(symbol: str, session: Session = Depends(get_session)) -> StockDeleteResponse:
    try:
        normalized_symbol = normalize_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    stock = session.scalar(select(Stock).where(Stock.symbol == normalized_symbol))
    if not stock:
        raise HTTPException(status_code=404, detail="Stock not found")

    await refresh_manager.forget_symbol(normalized_symbol)

    broker_trading = session.scalar(
        select(StockBrokerTrading).where(StockBrokerTrading.stock_id == stock.id)
    )
    if broker_trading:
        session.execute(delete(StockBrokerTradingRow).where(StockBrokerTradingRow.broker_trading_id == broker_trading.id))
        session.delete(broker_trading)
    session.execute(delete(StockPosition).where(StockPosition.stock_id == stock.id))
    session.execute(delete(StockMetric).where(StockMetric.stock_id == stock.id))
    session.execute(delete(StockEPS).where(StockEPS.stock_id == stock.id))
    session.execute(delete(StockValuation).where(StockValuation.stock_id == stock.id))
    session.execute(delete(StockDailyPrice).where(StockDailyPrice.stock_id == stock.id))
    session.execute(delete(StockPEHistory).where(StockPEHistory.stock_id == stock.id))
    session.execute(delete(StockMonthlyRevenue).where(StockMonthlyRevenue.stock_id == stock.id))
    session.execute(delete(StockFinancialQuarter).where(StockFinancialQuarter.stock_id == stock.id))
    session.execute(delete(StockAIAnalysis).where(StockAIAnalysis.stock_id == stock.id))
    session.execute(delete(StockRefreshState).where(StockRefreshState.symbol == normalized_symbol))
    session.execute(delete(CrawlerLog).where(CrawlerLog.job_name == f"market_refresh:{normalized_symbol}"))
    session.delete(stock)
    session.commit()
    return StockDeleteResponse(status="ok", symbol=normalized_symbol)


@app.put("/api/stocks/{symbol}/position", response_model=StockResponse)
def set_stock_position(
    symbol: str,
    payload: StockPositionRequest,
    session: Session = Depends(get_session),
) -> StockResponse:
    try:
        normalized_symbol = normalize_symbol(symbol)
        buy_price = _positive_money(payload.buy_price)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    stock = session.scalar(select(Stock).where(Stock.symbol == normalized_symbol, Stock.is_active.is_(True)))
    if not stock:
        raise HTTPException(status_code=404, detail="Stock not found")

    now = datetime.now(UTC)
    position = session.scalar(select(StockPosition).where(StockPosition.stock_id == stock.id))
    if not position:
        position = StockPosition(stock_id=stock.id, buy_price=buy_price)
        session.add(position)
    else:
        position.buy_price = buy_price
        position.updated_at = now

    stock.updated_at = now
    session.commit()
    return _stock_response(stock, session)


@app.delete("/api/stocks/{symbol}/position", response_model=StockResponse)
def clear_stock_position(symbol: str, session: Session = Depends(get_session)) -> StockResponse:
    try:
        normalized_symbol = normalize_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    stock = session.scalar(select(Stock).where(Stock.symbol == normalized_symbol, Stock.is_active.is_(True)))
    if not stock:
        raise HTTPException(status_code=404, detail="Stock not found")

    position = session.scalar(select(StockPosition).where(StockPosition.stock_id == stock.id))
    if position:
        session.delete(position)
        stock.updated_at = datetime.now(UTC)
        session.commit()

    return _stock_response(stock, session)


@app.get("/api/stocks/{symbol}/valuations", response_model=list[StockValuationResponse])
def get_stock_valuations(symbol: str, session: Session = Depends(get_session)) -> list[StockValuationResponse]:
    stock = session.scalar(select(Stock).where(Stock.symbol == symbol))
    if not stock:
        raise HTTPException(status_code=404, detail="Stock not found")

    valuations = session.scalars(
        select(StockValuation)
        .where(StockValuation.stock_id == stock.id)
        .order_by(StockValuation.eps_type.desc())
    ).all()
    position = session.scalar(select(StockPosition).where(StockPosition.stock_id == stock.id))
    return [_valuation_response(valuation, position.buy_price if position else None) for valuation in valuations]


@app.post("/api/stocks/{symbol}/refresh", response_model=RefreshQueueResponse, status_code=status.HTTP_202_ACCEPTED)
async def refresh_stock(symbol: str) -> RefreshQueueResponse:
    try:
        state = await refresh_manager.queue_symbol(symbol, create_placeholder=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RefreshQueueResponse(
        status=state.status,
        symbol=state.symbol,
        queued_at=state.queued_at or datetime.now(UTC),
        message=state.message,
    )
