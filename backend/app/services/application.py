from __future__ import annotations

import csv
import io
import json
import threading
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from ..brokers import BrokerConfig, broker_options, get_broker, transaction_tax_rate
from ..ai_analysis import (
    AI_MODE_HELD,
    AI_MODE_UNHELD,
    PROMPT_VERSION,
    AIAnalysisError,
    AIConfigurationError,
    GeminiProvider,
    OpenRouterProvider,
    ai_provider_identity,
    build_ai_provider,
    grounding_errors_from_validation_errors,
    normalize_ai_analysis,
    normalize_ai_analysis_with_errors,
    quality_flags_from_validation_errors,
    stock_summary_hash,
)
from ..config import get_settings
from ..db.models import (
    CrawlerLog,
    Stock,
    StockAIAnalysis,
    StockAIFeedback,
    StockBrokerTrading,
    StockBrokerTradingRow,
    StockDailyPrice,
    StockDataQualityState,
    StockEPS,
    StockFinancialQuarter,
    StockMetric,
    StockMonthlyRevenue,
    StockPEHistory,
    StockPosition,
    StockRefreshState,
    StockValuation,
)
from ..db.bootstrap import get_app_setting, init_database, set_app_setting
from ..db.session import DATABASE_URL, SessionLocal, get_session, ping_database
from ..schemas import (
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
    StockAIAnalysisFeedbackRequest,
    StockAIAnalysisFeedbackResponse,
    StockAIAnalysisRequest,
    StockAIAnalysisModesResponse,
    StockAIAnalysisResultResponse,
    StockAIAnalysisResponse,
    StockAIAnalysisContent,
    StockAIAnalysisEvidenceText,
    StockAIRuleBasedModesResponse,
    StockAIRuleBasedResultResponse,
    StockMetricResponse,
    DataQualityCategorySummaryResponse,
    DataQualityComponentResponse,
    DataQualityItemResponse,
    DataQualitySummaryResponse,
    StockDataQualityResponse,
    StockPositionRequest,
    StockPositionResponse,
    StockReorderRequest,
    StockResponse,
    StockValuationResponse,
    TechnicalAnalysisResponse,
    TechnicalCandleResponse,
)
from ..data_quality import as_taipei as quality_as_taipei, freshness_for_state
from ..refresh.manager import BackgroundRefreshManager
from ..taifex_futures import latest_wtx_response
from ..technical import MOVING_AVERAGE_PERIODS, moving_averages
from ..valuation import quantize_money, valuation_status
from .market_data_service import normalize_symbol


settings = get_settings()
refresh_manager = BackgroundRefreshManager(
    interval_seconds=settings.background_refresh_seconds,
    finmind_token=settings.finmind_token,
    quote_market_interval_seconds=settings.quote_market_interval_seconds,
    quote_off_hours_interval_seconds=settings.quote_off_hours_interval_seconds,
    pe_poll_interval_seconds=settings.pe_poll_interval_seconds,
    monthly_revenue_release_interval_seconds=settings.monthly_revenue_release_interval_seconds,
    futures_refresh_seconds=settings.futures_refresh_seconds,
)

TAIPEI_TZ = ZoneInfo("Asia/Taipei")
MARKET_OPEN_TIME = time(9, 0)
MARKET_CLOSE_TIME = time(14, 0)
AI_ANALYSIS_INFLIGHT_TIMEOUT = timedelta(minutes=10)
AI_ANALYSIS_JOB_LOCK = threading.Lock()


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


def _stock_response(stock: Stock, session: Session, *, include_data_quality: bool = True) -> StockResponse:
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
        data_quality_summary=(
            _stock_data_quality_response(stock, session, summary_only=True)
            if include_data_quality
            else None
        ),
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
    ma_distance_values = {
        "price_vs_ma5_percent": _ratio_percent(latest.close, latest.ma5),
        "price_vs_ma10_percent": _ratio_percent(latest.close, latest.ma10),
        "price_vs_ma20_percent": _ratio_percent(latest.close, latest.ma20),
        "price_vs_ma60_percent": _ratio_percent(latest.close, latest.ma60),
        "price_vs_ma120_percent": _ratio_percent(latest.close, latest.ma120),
        "price_vs_ma240_percent": _ratio_percent(latest.close, latest.ma240),
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
            **ma_distance_values,
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


def _quote_context_for_ai(metric) -> dict | None:
    if metric is None:
        return None
    current_price = metric.current_price
    return {
        "current_price_twd": current_price,
        "open_price_twd": metric.open_price,
        "previous_close_twd": metric.previous_close,
        "day_high_twd": metric.day_high,
        "day_low_twd": metric.day_low,
        "current_vs_open_percent": _ratio_percent(current_price, metric.open_price),
        "current_vs_previous_close_percent": _ratio_percent(current_price, metric.previous_close),
        "current_vs_day_high_percent": _ratio_percent(current_price, metric.day_high),
        "current_vs_day_low_percent": _ratio_percent(current_price, metric.day_low),
        "price_updated_at": metric.price_updated_at.isoformat(),
        "source": metric.source,
    }


def _pe_context_for_ai(metric) -> dict | None:
    if metric is None:
        return None
    pe_range_position_percent = None
    if (
        metric.current_pe is not None
        and metric.pe_min_3y is not None
        and metric.pe_max_3y is not None
        and metric.pe_max_3y != metric.pe_min_3y
    ):
        pe_range_position_percent = round(
            (metric.current_pe - metric.pe_min_3y) / (metric.pe_max_3y - metric.pe_min_3y) * 100,
            2,
        )
    return {
        "current_pe": metric.current_pe,
        "pe_average_3y": metric.pe_average_3y,
        "pe_min_3y": metric.pe_min_3y,
        "pe_max_3y": metric.pe_max_3y,
        "current_pe_vs_average_percent": metric.pe_vs_average_percent,
        "current_pe_position_in_3y_range_percent": pe_range_position_percent,
        "pe_updated_at": metric.pe_updated_at.isoformat(),
        "pe_data_date": (
            getattr(metric, "pe_data_date", None).isoformat()
            if getattr(metric, "pe_data_date", None)
            else None
        ),
    }


def _broker_rows_for_ai(rows) -> list[dict]:
    return [
        {
            "rank": row.rank,
            "broker_name": row.broker_name,
            "buy_volume_lots": abs(row.buy_volume),
            "sell_volume_lots": abs(row.sell_volume),
            "net_volume_lots": row.net_volume,
        }
        for row in rows[:5]
    ]


def _ai_evidence_from_summary(summary: dict) -> dict[str, object]:
    evidence: dict[str, object] = {}
    aliases = {
        "pe_context": "valuation",
    }

    def add(prefix: str, value) -> None:
        if value is None:
            return
        if isinstance(value, dict):
            for key, nested in value.items():
                if key in {"source", "fetched_at"} and prefix:
                    continue
                add(f"{prefix}.{key}" if prefix else str(key), nested)
            return
        if isinstance(value, list):
            for index, nested in enumerate(value):
                add(f"{prefix}.{index}", nested)
            return
        if prefix:
            evidence[prefix] = value

    for top_key, value in summary.items():
        if top_key in {"evidence", "available_evidence_keys", "summary_version", "prompt_version", "analysis_mode"}:
            continue
        evidence_key = aliases.get(top_key, top_key)
        add(evidence_key, value)
    return evidence


def _ai_stock_summary(stock: Stock, session: Session, analysis_mode: str) -> dict:
    stock_response = _stock_response(stock, session, include_data_quality=False)
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
        "quote": _quote_context_for_ai(metric),
        "pe_context": _pe_context_for_ai(metric),
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
            "top_buy_brokers": _broker_rows_for_ai(broker_trading.buy_brokers),
            "top_sell_brokers": _broker_rows_for_ai(broker_trading.sell_brokers),
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
    evidence = _ai_evidence_from_summary(summary)
    summary["evidence"] = evidence
    summary["available_evidence_keys"] = sorted(evidence)
    return summary


def _ai_analysis_context(now: datetime | None = None) -> dict[str, str]:
    analysis_now = now or datetime.now(TAIPEI_TZ)
    if analysis_now.tzinfo is None:
        analysis_now = analysis_now.replace(tzinfo=TAIPEI_TZ)
    else:
        analysis_now = analysis_now.astimezone(TAIPEI_TZ)
    return {
        "analysis_requested_at": analysis_now.isoformat(timespec="seconds"),
        "local_date": analysis_now.date().isoformat(),
        "local_time": analysis_now.time().replace(tzinfo=None).isoformat(timespec="seconds"),
        "timezone": "Asia/Taipei",
    }


def _ai_cache_input_hash(stock_summary: dict) -> str:
    stable_summary = dict(stock_summary)
    stable_summary.pop("analysis_context", None)
    evidence = stable_summary.get("evidence")
    if isinstance(evidence, dict):
        stable_summary["evidence"] = {
            key: value
            for key, value in evidence.items()
            if not str(key).startswith("analysis_context.")
        }
    return stock_summary_hash(stable_summary)


def _compact_ai_stock_summary(
    summary: dict,
    analysis_mode: str,
    analysis_now: datetime | None = None,
) -> dict:
    """Keep provider input focused; full summaries remain available in local logs."""
    compact = {
        "symbol": summary.get("symbol"),
        "name": summary.get("name"),
        "asset_type": summary.get("asset_type"),
        "market": summary.get("market"),
        "currency": summary.get("currency"),
        "summary_version": 3,
        "prompt_version": PROMPT_VERSION,
        "analysis_mode": analysis_mode,
        "analysis_context": _ai_analysis_context(analysis_now),
        "quote": _compact_dict(
            summary.get("quote"),
            (
                "current_price_twd",
                "open_price_twd",
                "previous_close_twd",
                "day_high_twd",
                "day_low_twd",
                "current_vs_open_percent",
                "current_vs_previous_close_percent",
                "current_vs_day_high_percent",
                "current_vs_day_low_percent",
                "price_updated_at",
            ),
        ),
        "pe_context": _compact_dict(
            summary.get("pe_context"),
            (
                "current_pe",
                "pe_average_3y",
                "pe_min_3y",
                "pe_max_3y",
                "current_pe_vs_average_percent",
                "current_pe_position_in_3y_range_percent",
                "pe_data_date",
            ),
        ),
        "valuation_scenarios": [
            _compact_dict(
                scenario,
                (
                    "eps_type",
                    "eps_value",
                    "mechanical_eps_times_current_pe_price_twd",
                    "mechanical_price_vs_current_price_percent",
                    "mechanical_price_vs_average_cost_percent",
                    "scenario_label",
                ),
            )
            for scenario in (summary.get("valuation_scenarios") or [])[:2]
        ],
        "fundamental": _compact_dict(
            summary.get("fundamental"),
            (
                "latest_quarter_eps",
                "eps_yoy_percent",
                "ttm_eps_yoy_percent",
                "latest_revenue_yoy_percent",
                "latest_revenue_mom_percent",
                "three_month_revenue_yoy_percent",
                "gross_margin",
                "gross_margin_sos",
                "operating_margin",
                "operating_margin_sos",
                "net_margin",
                "net_margin_sos",
                "fetched_at",
            ),
        ),
        "technical": {
            "latest": _compact_dict(
                (summary.get("technical") or {}).get("latest"),
                (
                    "date",
                    "close_price_twd",
                    "is_provisional",
                    "ma5",
                    "ma10",
                    "ma20",
                    "ma60",
                    "ma120",
                    "ma240",
                    "price_vs_ma5_percent",
                    "price_vs_ma10_percent",
                    "price_vs_ma20_percent",
                    "price_vs_ma60_percent",
                    "price_vs_ma120_percent",
                    "price_vs_ma240_percent",
                    "today_volume_lots",
                    "volume_ma5_lots",
                    "volume_ma20_lots",
                    "volume_as_percent_of_ma20",
                    "volume_difference_vs_ma20_percent",
                ),
            )
        },
        "chip": _compact_chip(summary.get("chip")),
    }
    if analysis_mode == AI_MODE_HELD and summary.get("position"):
        compact["position"] = _compact_dict(
            summary.get("position"),
            (
                "average_cost_price_twd",
                "unrealized_profit_loss_per_share_twd",
                "unrealized_return_percent",
                "fee_adjusted_profit_loss_per_share_twd",
                "fee_adjusted_return_percent",
                "broker_id",
                "broker_fee_rate",
            ),
        )
    compact["valuation_scenarios"] = [scenario for scenario in compact["valuation_scenarios"] if scenario]
    compact["technical"] = compact["technical"] if compact["technical"]["latest"] else None
    compact = {key: value for key, value in compact.items() if value not in (None, {}, [])}
    evidence = _ai_evidence_from_summary(compact)
    compact["evidence"] = evidence
    compact["available_evidence_keys"] = sorted(evidence)
    return compact


def _compact_dict(value, keys: tuple[str, ...]) -> dict:
    if not isinstance(value, dict):
        return {}
    return {key: value.get(key) for key in keys if value.get(key) is not None}


def _compact_chip(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    compact = _compact_dict(
        value,
        (
            "trade_date",
            "main_net_volume_lots",
            "main_buy_volume_lots",
            "main_sell_volume_lots",
            "volume_ratio_percent",
        ),
    )
    buy_rows = value.get("top_buy_brokers") or []
    sell_rows = value.get("top_sell_brokers") or []
    if buy_rows:
        compact["top_buy_brokers"] = [
            _compact_dict(
                row,
                ("rank", "broker_name", "buy_volume_lots", "sell_volume_lots", "net_volume_lots"),
            )
            for row in buy_rows[:3]
        ]
    if sell_rows:
        compact["top_sell_brokers"] = [
            _compact_dict(
                row,
                ("rank", "broker_name", "buy_volume_lots", "sell_volume_lots", "net_volume_lots"),
            )
            for row in sell_rows[:3]
        ]
    return compact or None


QUALITY_LABELS = {
    "QUOTE": "行情",
    "CURRENT_PE": "目前 PE",
    "PE_HISTORY": "三年 PE",
    "EPS": "EPS",
    "FINANCIAL_QUARTER": "季度財報",
    "MONTHLY_REVENUE": "月營收",
    "BROKER_TRADING": "籌碼",
    "TECHNICAL_DAILY": "技術日線",
    "AI_UNHELD": "未持有分析",
    "AI_HELD": "持有中分析",
}
QUALITY_SEVERITY = {
    "NOT_APPLICABLE": -1,
    "REALTIME": 0,
    "CURRENT": 0,
    "DELAYED": 1,
    "STALE": 2,
    "MISSING": 2,
}


def _quality_api_datetime(value: datetime | None, now: datetime) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(UTC)
    utc_candidate = value.replace(tzinfo=UTC)
    if utc_candidate > now.astimezone(UTC) + timedelta(minutes=5):
        return value.replace(tzinfo=TAIPEI_TZ).astimezone(UTC)
    return utc_candidate


def _quality_sync_status(state: StockDataQualityState | None) -> str:
    if state is None:
        return "idle"
    if state.sync_status in {"queued", "running", "retry_wait", "failed"}:
        return state.sync_status
    if state.last_error_at and (not state.last_success_at or quality_as_taipei(state.last_error_at) >= quality_as_taipei(state.last_success_at)):
        return "retry_wait" if state.next_retry_at else "failed"
    return "success" if state.last_success_at else "idle"


def _quality_component(
    session: Session,
    stock: Stock,
    state: StockDataQualityState | None,
    category: str,
    *,
    now: datetime,
    applicable: bool = True,
) -> DataQualityComponentResponse:
    freshness = freshness_for_state(session, state, category, now=now, applicable=applicable)
    return DataQualityComponentResponse(
        category=category,
        label=QUALITY_LABELS.get(category, category),
        freshness_status=freshness,
        is_cached=bool(state and state.is_cached),
        sync_status=_quality_sync_status(state),
        data_date=state.data_date if state else None,
        data_period=state.data_period if state else None,
        fetched_at=_quality_api_datetime(state.fetched_at, now) if state else None,
        source=state.source if state else None,
        last_attempt_at=_quality_api_datetime(state.last_attempt_at, now) if state else None,
        last_success_at=_quality_api_datetime(state.last_success_at, now) if state else None,
        last_error_summary=state.last_error_summary if state else None,
        last_error_detail=state.last_error_detail if state else None,
        last_error_at=_quality_api_datetime(state.last_error_at, now) if state else None,
        next_retry_at=_quality_api_datetime(state.next_retry_at, now) if state else None,
    )


def _worst_quality(components: list[DataQualityComponentResponse]) -> str:
    applicable = [item for item in components if item.freshness_status != "NOT_APPLICABLE"]
    if not applicable:
        return "NOT_APPLICABLE"
    return max(applicable, key=lambda item: QUALITY_SEVERITY.get(item.freshness_status, 2)).freshness_status


def _aggregate_quality_item(
    category: str,
    label: str,
    components: list[DataQualityComponentResponse],
) -> DataQualityItemResponse:
    applicable = [item for item in components if item.freshness_status != "NOT_APPLICABLE"]
    primary = max(applicable or components, key=lambda item: QUALITY_SEVERITY.get(item.freshness_status, -1))
    errors = [item for item in components if item.last_error_at]
    latest_error = max(errors, key=lambda item: quality_as_taipei(item.last_error_at)) if errors else None
    return DataQualityItemResponse(
        category=category,
        label=label,
        freshness_status=_worst_quality(components),
        is_cached=any(item.is_cached for item in applicable),
        sync_status=(
            "retry_wait" if any(item.sync_status == "retry_wait" for item in applicable)
            else "failed" if any(item.sync_status == "failed" for item in applicable)
            else "success" if applicable and all(item.sync_status == "success" for item in applicable)
            else "idle"
        ),
        data_date=primary.data_date,
        data_period=primary.data_period,
        fetched_at=primary.fetched_at,
        source=primary.source,
        last_attempt_at=max((item.last_attempt_at for item in applicable if item.last_attempt_at), default=None),
        last_success_at=max((item.last_success_at for item in applicable if item.last_success_at), default=None),
        last_error_summary=latest_error.last_error_summary if latest_error else None,
        last_error_detail=latest_error.last_error_detail if latest_error else None,
        last_error_at=latest_error.last_error_at if latest_error else None,
        next_retry_at=min((item.next_retry_at for item in applicable if item.next_retry_at), default=None),
        components=components,
    )


def _ai_quality_components(stock: Stock, session: Session, now: datetime) -> list[DataQualityComponentResponse]:
    modes = [AI_MODE_UNHELD]
    if session.scalar(select(StockPosition.id).where(StockPosition.stock_id == stock.id)):
        modes.append(AI_MODE_HELD)
    components = []
    for mode in modes:
        rows = session.scalars(
            select(StockAIAnalysis)
            .where(StockAIAnalysis.stock_id == stock.id, StockAIAnalysis.analysis_mode == mode)
            .order_by(StockAIAnalysis.updated_at.desc())
            .limit(20)
        ).all()
        success = next((row for row in rows if _ai_analysis_is_cacheable(row)), None)
        latest_attempt = rows[0] if rows else None
        latest_attempt_after_success = bool(
            latest_attempt
            and success
            and latest_attempt.id != success.id
            and quality_as_taipei(latest_attempt.updated_at) >= quality_as_taipei(success.updated_at)
        )
        failed_after_success = bool(
            latest_attempt_after_success
            and latest_attempt.status in {"failed", "format_fallback"}
        )
        updating_after_success = bool(
            latest_attempt_after_success
            and latest_attempt.status in {"queued", "running"}
        )
        snapshot_matches_current_data = True
        if success and getattr(success, "run", None) is not None:
            saved_items = _json_field(success.run.data_as_of_json) or []
            current_states = {
                state.category: state
                for state in session.scalars(
                    select(StockDataQualityState).where(StockDataQualityState.stock_id == stock.id)
                ).all()
            }
            for saved_item in saved_items:
                state = current_states.get(saved_item.get("category"))
                current_date = state.data_date.isoformat() if state and state.data_date else None
                current_period = state.data_period if state else None
                if (
                    current_date != saved_item.get("data_date")
                    or current_period != saved_item.get("data_period")
                ):
                    snapshot_matches_current_data = False
                    break
        current = bool(
            success
            and success.prompt_version == PROMPT_VERSION
            and success.analysis_date == quality_as_taipei(now).date()
            and not failed_after_success
            and snapshot_matches_current_data
        )
        latest_failed = bool(latest_attempt and latest_attempt.status in {"failed", "format_fallback"})
        sync_status = (
            "failed" if latest_failed
            else latest_attempt.status if latest_attempt
            else "idle"
        )
        error_summary = None
        if latest_attempt and latest_attempt.status == "failed":
            error_summary = "AI 分析服務暫時失敗"
        elif latest_attempt and latest_attempt.status == "format_fallback":
            error_summary = "AI 回覆未通過格式驗證"
        category = "AI_HELD" if mode == AI_MODE_HELD else "AI_UNHELD"
        components.append(
            DataQualityComponentResponse(
                category=category,
                label=QUALITY_LABELS[category],
                freshness_status="MISSING" if success is None else "CURRENT" if current else "STALE",
                is_cached=bool(success and (failed_after_success or updating_after_success)),
                sync_status=sync_status,
                data_date=success.analysis_date if success else None,
                data_period=success.prompt_version if success else None,
                fetched_at=(
                    _quality_api_datetime(_ai_row_analysis_requested_at(success) or success.updated_at, now)
                    if success else None
                ),
                source=f"{success.provider} · {success.model}" if success else None,
                last_attempt_at=_quality_api_datetime(latest_attempt.updated_at, now) if latest_attempt else None,
                last_success_at=_quality_api_datetime(success.updated_at, now) if success else None,
                last_error_summary=error_summary if latest_failed else None,
                last_error_detail=latest_attempt.error_message if latest_failed else None,
                last_error_at=(
                    _quality_api_datetime(latest_attempt.updated_at, now)
                    if latest_failed else None
                ),
            )
        )
    return components


def _stock_data_quality_response(
    stock: Stock,
    session: Session,
    *,
    summary_only: bool = False,
) -> StockDataQualityResponse | DataQualitySummaryResponse:
    now = datetime.now(UTC)
    states = {
        state.category: state
        for state in session.scalars(
            select(StockDataQualityState).where(StockDataQualityState.stock_id == stock.id)
        ).all()
    }
    is_etf = stock.asset_type == "ETF"
    eps_count = session.scalar(select(func.count()).select_from(StockEPS).where(StockEPS.stock_id == stock.id)) or 0
    positive_eps_count = session.scalar(
        select(func.count()).select_from(StockEPS).where(StockEPS.stock_id == stock.id, StockEPS.eps_value > 0)
    ) or 0
    pe_applicable = not is_etf and not (eps_count > 0 and positive_eps_count == 0)
    direct = {
        category: _quality_component(
            session,
            stock,
            states.get(category),
            category,
            now=now,
            applicable=(
                pe_applicable if category in {"CURRENT_PE", "PE_HISTORY"}
                else not is_etf if category in {"EPS", "FINANCIAL_QUARTER", "MONTHLY_REVENUE"}
                else True
            ),
        )
        for category in (
            "QUOTE", "CURRENT_PE", "PE_HISTORY", "EPS", "FINANCIAL_QUARTER",
            "MONTHLY_REVENUE", "BROKER_TRADING", "TECHNICAL_DAILY",
        )
    }
    items = [
        _aggregate_quality_item("QUOTE", "行情", [direct["QUOTE"]]),
        _aggregate_quality_item("PE", "PE", [direct["CURRENT_PE"], direct["PE_HISTORY"]]),
        _aggregate_quality_item(
            "FUNDAMENTAL", "基本面",
            [direct["EPS"], direct["FINANCIAL_QUARTER"], direct["MONTHLY_REVENUE"]],
        ),
        _aggregate_quality_item("BROKER_TRADING", "籌碼", [direct["BROKER_TRADING"]]),
        _aggregate_quality_item("TECHNICAL_DAILY", "技術日線", [direct["TECHNICAL_DAILY"]]),
        _aggregate_quality_item("AI_ANALYSIS", "AI 分析", _ai_quality_components(stock, session, now)),
    ]
    applicable_items = [item for item in items if item.freshness_status != "NOT_APPLICABLE"]
    critical = [item for item in applicable_items if item.freshness_status in {"STALE", "MISSING"}]
    warnings = [
        item for item in applicable_items
        if item.freshness_status == "DELAYED" or item.is_cached or item.sync_status in {"queued", "running", "retry_wait", "failed"}
    ]
    overall = "CRITICAL" if critical else "WARNING" if warnings else "HEALTHY"
    issue_count = len({item.category for item in critical + warnings})
    if summary_only:
        return DataQualitySummaryResponse(
            overall_status=overall,
            issue_count=issue_count,
            categories={
                item.category: DataQualityCategorySummaryResponse(
                    freshness_status=item.freshness_status,
                    is_cached=item.is_cached,
                    sync_status=item.sync_status,
                )
                for item in items
            },
        )
    return StockDataQualityResponse(
        symbol=stock.symbol,
        overall_status=overall,
        issue_count=issue_count,
        checked_at=now,
        items=items,
    )


def _rule_based_ai_analysis(summary: dict, analysis_mode: str) -> StockAIAnalysisContent:
    evidence_keys = set((summary.get("evidence") or {}).keys())

    def grounded(text: str, keys: list[str]) -> StockAIAnalysisEvidenceText:
        valid = [key for key in keys if key in evidence_keys]
        return StockAIAnalysisEvidenceText(text=text, evidence_keys=valid[:4])

    quote = summary.get("quote") or {}
    pe = summary.get("pe_context") or {}
    fundamental = summary.get("fundamental") or {}
    technical_latest = ((summary.get("technical") or {}).get("latest") or {})
    chip = summary.get("chip") or {}
    scenarios = summary.get("valuation_scenarios") or []
    position = summary.get("position") or {}

    positive: list[StockAIAnalysisEvidenceText] = []
    risks: list[StockAIAnalysisEvidenceText] = []
    watch: list[StockAIAnalysisEvidenceText] = []

    current_price = quote.get("current_price_twd")
    if current_price is None:
        return StockAIAnalysisContent(
            overall_status="資料不足" if analysis_mode == AI_MODE_UNHELD else "觀察",
            summary=grounded("目前缺少現價資料，先以資料補齊與快取更新為優先。", []),
            positive_points=[],
            risk_points=[grounded("現價資料尚未建立，無法形成完整判斷。", [])],
            watch_points=[grounded("等待行情、估值、基本面與技術資料更新後再重新分析。", [])],
            disclaimer=DEFAULT_AI_DISCLAIMER,
        )

    pe_position = pe.get("current_pe_position_in_3y_range_percent")
    pe_vs_avg = pe.get("current_pe_vs_average_percent")
    if pe_position is not None:
        if pe_position >= 80:
            risks.append(grounded("目前 PE 位於近三年偏高區間，估值安全邊際較低。", ["valuation.current_pe_position_in_3y_range_percent"]))
        elif pe_position <= 30:
            positive.append(grounded("目前 PE 位於近三年偏低區間，估值壓力相對較小。", ["valuation.current_pe_position_in_3y_range_percent"]))
    if pe_vs_avg is not None and pe_vs_avg > 20:
        risks.append(grounded("目前 PE 明顯高於三年平均，需基本面持續支撐。", ["valuation.current_pe_vs_average_percent"]))

    best_scenario = next(
        (
            scenario
            for scenario in scenarios
            if scenario.get("mechanical_price_vs_current_price_percent") is not None
        ),
        None,
    )
    if best_scenario:
        scenario_diff = best_scenario["mechanical_price_vs_current_price_percent"]
        if scenario_diff >= 10:
            positive.append(grounded("EPS × 目前 PE 的機械情境高於現價，估值情境仍有支撐。", ["valuation_scenarios.0.mechanical_price_vs_current_price_percent"]))
        elif scenario_diff <= -10:
            risks.append(grounded("EPS × 目前 PE 的機械情境低於現價，需留意估值偏高。", ["valuation_scenarios.0.mechanical_price_vs_current_price_percent"]))

    eps_yoy = fundamental.get("eps_yoy_percent")
    revenue_yoy = fundamental.get("latest_revenue_yoy_percent")
    ttm_eps_yoy = fundamental.get("ttm_eps_yoy_percent")
    if eps_yoy is not None and eps_yoy > 0:
        positive.append(grounded("最新單季 EPS 年增為正，獲利動能有改善訊號。", ["fundamental.eps_yoy_percent"]))
    if revenue_yoy is not None and revenue_yoy > 0:
        positive.append(grounded("最新月營收年增為正，營收面仍有成長支撐。", ["fundamental.latest_revenue_yoy_percent"]))
    if ttm_eps_yoy is not None and ttm_eps_yoy < 0:
        risks.append(grounded("TTM EPS 年增為負，獲利趨勢仍需追蹤。", ["fundamental.ttm_eps_yoy_percent"]))

    price_vs_ma20 = technical_latest.get("price_vs_ma20_percent")
    price_vs_ma60 = technical_latest.get("price_vs_ma60_percent")
    if price_vs_ma20 is not None and price_vs_ma20 > 0:
        positive.append(grounded("收盤價高於 MA20，短線技術面偏強。", ["technical.latest.price_vs_ma20_percent"]))
    elif price_vs_ma20 is not None:
        risks.append(grounded("收盤價低於 MA20，短線動能偏弱。", ["technical.latest.price_vs_ma20_percent"]))
    if price_vs_ma60 is not None and price_vs_ma60 < 0:
        risks.append(grounded("收盤價低於 MA60，中期技術面需重新觀察。", ["technical.latest.price_vs_ma60_percent"]))

    volume_diff = technical_latest.get("volume_difference_vs_ma20_percent")
    if volume_diff is not None:
        if volume_diff >= 20:
            positive.append(grounded("今日量高於 20 日均量，短線關注度提高。", ["technical.latest.volume_difference_vs_ma20_percent"]))
        elif volume_diff <= -40:
            watch.append(grounded("今日量明顯低於 20 日均量，需觀察後續量能是否回升。", ["technical.latest.volume_difference_vs_ma20_percent"]))

    main_net = chip.get("main_net_volume_lots")
    if main_net is not None:
        if main_net > 0:
            positive.append(grounded("主力買賣超為正，籌碼面有偏多訊號。", ["chip.main_net_volume_lots"]))
        elif main_net < 0:
            risks.append(grounded("主力買賣超為負，籌碼面短線偏保守。", ["chip.main_net_volume_lots"]))

    if analysis_mode == AI_MODE_HELD:
        return_pct = position.get("unrealized_return_percent")
        fee_return_pct = position.get("fee_adjusted_return_percent")
        if fee_return_pct is not None and fee_return_pct > 0:
            positive.insert(0, grounded("費後損益估算為正，目前持有部位仍有獲利緩衝。", ["position.fee_adjusted_return_percent"]))
        elif fee_return_pct is not None:
            risks.insert(0, grounded("費後損益估算為負，持有成本壓力需要納入判斷。", ["position.fee_adjusted_return_percent"]))
        if return_pct is not None and return_pct > 15 and len(risks) >= 2:
            status_text = "分批調節"
        elif len(risks) >= 3 and (price_vs_ma60 is not None and price_vs_ma60 < 0):
            status_text = "重新評估"
        elif len(positive) >= 3 and len(risks) <= 1:
            status_text = "續抱"
        else:
            status_text = "觀察"
        summary_text = "持有面需同時看獲利緩衝、估值位置、技術趨勢與籌碼方向；目前以保守檢視持有理由為主。"
    else:
        if len(risks) >= 3 and not positive:
            status_text = "避開"
        elif len(positive) >= 3 and len(risks) <= 1:
            status_text = "分批布局"
        elif not positive and not risks:
            status_text = "資料不足"
        else:
            status_text = "等待"
        summary_text = "未持有評估需避免追高，先看估值位置、基本面延續性、技術趨勢與籌碼是否形成同向訊號。"

    quality_context = summary.get("data_quality_context") or {}
    quality_items = quality_context.get("items") or []
    critical_quality = [
        item for item in quality_items
        if item.get("freshness_status") in {"STALE", "MISSING"}
    ]
    if critical_quality:
        labels = "、".join(str(item.get("label") or item.get("category")) for item in critical_quality[:3])
        watch.insert(
            0,
            grounded(
                f"{labels}資料過期或缺失，本次判斷信心已降低，更新後應重新分析。",
                [f"data_quality.{str(item.get('category')).lower()}.freshness_status" for item in critical_quality[:3]],
            ),
        )
        if analysis_mode == AI_MODE_UNHELD and status_text == "分批布局":
            status_text = "等待"
        elif analysis_mode == AI_MODE_HELD and status_text == "續抱":
            status_text = "觀察"

    watch.append(grounded("後續優先追蹤 PE 位置、月營收與 EPS 趨勢、MA20/MA60 以及主力買賣超是否同向。", []))
    return StockAIAnalysisContent(
        overall_status=status_text,
        summary=grounded(summary_text, []),
        positive_points=positive[:3],
        risk_points=risks[:3],
        watch_points=watch[:3],
        disclaimer=DEFAULT_AI_DISCLAIMER,
    )


DEFAULT_AI_DISCLAIMER = "規則摘要僅依據本機快取資料整理，不構成任何投資建議。"


def _rule_based_result_response(stock: Stock, session: Session, analysis_mode: str) -> StockAIRuleBasedResultResponse:
    summary = _compact_ai_stock_summary(_ai_stock_summary(stock, session, analysis_mode), analysis_mode)
    return StockAIRuleBasedResultResponse(
        mode=analysis_mode,
        generated_at=datetime.now(UTC),
        analysis=_rule_based_ai_analysis(summary, analysis_mode),
    )


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
        raise RuntimeError("Cached AI analysis is invalid.") from exc
    evidence_keys = _ai_row_evidence_keys(row)
    return StockAIAnalysisResultResponse(
        id=row.id,
        mode=row.analysis_mode,
        provider=row.provider,
        model=row.model,
        prompt_version=row.prompt_version,
        cached=cached,
        analysis_date=row.analysis_date,
        analysis_requested_at=_ai_row_analysis_requested_at(row),
        generated_at=row.updated_at,
        analysis=normalize_ai_analysis(analysis_payload, row.analysis_mode, evidence_keys=evidence_keys),
    )


def _ai_row_analysis_requested_at(row: StockAIAnalysis) -> datetime | None:
    try:
        payload = json.loads(row.request_payload_json)
    except (TypeError, json.JSONDecodeError):
        return None
    context = payload.get("analysis_context") if isinstance(payload, dict) else None
    raw_value = context.get("analysis_requested_at") if isinstance(context, dict) else None
    if not isinstance(raw_value, str):
        return None
    try:
        return datetime.fromisoformat(raw_value)
    except ValueError:
        return None


def _ai_row_evidence_keys(row: StockAIAnalysis) -> set[str] | None:
    try:
        payload = json.loads(row.request_payload_json)
    except (TypeError, json.JSONDecodeError):
        return None
    evidence = payload.get("evidence") if isinstance(payload, dict) else None
    if not isinstance(evidence, dict):
        return None
    return {str(key) for key in evidence.keys()}


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


def _unique_models(models: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for model in models:
        normalized = (model or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique


def _ai_provider_error_quality_flags(error_message: str) -> list[str]:
    quality_flags = ["provider_error"]
    lower_error = error_message.lower()
    if "status 429" in lower_error or "rate limit" in lower_error or "too many requests" in lower_error:
        quality_flags.append("provider_rate_limited")
    if any(
        token in lower_error
        for token in ("status 500", "status 502", "status 503", "status 504", "bad gateway", "high demand")
    ):
        quality_flags.append("provider_outage")
    return sorted(set(quality_flags))


def _ai_model_recent_failure(
    session: Session,
    *,
    provider_id: str,
    model: str,
    analysis_mode: str,
    cooldown_seconds: int,
) -> StockAIAnalysis | None:
    cutoff = datetime.now(UTC) - timedelta(seconds=cooldown_seconds)
    return session.scalar(
        select(StockAIAnalysis)
        .where(
            StockAIAnalysis.provider == provider_id,
            StockAIAnalysis.model == model,
            StockAIAnalysis.analysis_mode == analysis_mode,
            StockAIAnalysis.prompt_version == PROMPT_VERSION,
            StockAIAnalysis.status == "failed",
            StockAIAnalysis.updated_at >= cutoff,
        )
        .order_by(StockAIAnalysis.updated_at.desc())
        .limit(1)
    )


def _ai_model_recent_format_failure(
    session: Session,
    *,
    provider_id: str,
    model: str,
    analysis_mode: str,
    cooldown_seconds: int,
) -> StockAIAnalysis | None:
    cutoff = datetime.now(UTC) - timedelta(seconds=cooldown_seconds)
    candidates = session.scalars(
        select(StockAIAnalysis)
        .where(
            StockAIAnalysis.provider == provider_id,
            StockAIAnalysis.model == model,
            StockAIAnalysis.analysis_mode == analysis_mode,
            StockAIAnalysis.prompt_version == PROMPT_VERSION,
            StockAIAnalysis.status == "format_fallback",
            StockAIAnalysis.updated_at >= cutoff,
        )
        .order_by(StockAIAnalysis.updated_at.desc())
        .limit(5)
    ).all()
    for row in candidates:
        flags = set(_json_field(row.quality_flags_json) or [])
        metadata = _json_field(row.provider_metadata_json) or {}
        finish_reason = str(metadata.get("finish_reason") or metadata.get("native_finish_reason") or "").lower()
        if flags & {"format_issue", "malformed_content"} or finish_reason == "length":
            return row
    return None


def _setting_positive_int(name: str, default: int) -> int:
    value = getattr(settings, name, default)
    return value if isinstance(value, int) and value > 0 else default


def _ai_model_in_cooldown(session: Session, *, provider_id: str, model: str, analysis_mode: str) -> bool:
    legacy_provider_cooldown_seconds = _setting_positive_int("openrouter_model_cooldown_seconds", 600)
    rate_limit_cooldown_seconds = _setting_positive_int("ai_rate_limit_cooldown_seconds", legacy_provider_cooldown_seconds)
    outage_cooldown_seconds = _setting_positive_int("ai_outage_cooldown_seconds", 180)
    format_cooldown_seconds = _setting_positive_int("ai_format_failure_cooldown_seconds", 1800)
    provider_cooldowns = (
        ("provider_rate_limited", rate_limit_cooldown_seconds),
        ("provider_outage", outage_cooldown_seconds),
    )
    for flag, cooldown_seconds in provider_cooldowns:
        if cooldown_seconds <= 0:
            continue
        row = _ai_model_recent_failure(
            session,
            provider_id=provider_id,
            model=model,
            analysis_mode=analysis_mode,
            cooldown_seconds=cooldown_seconds,
        )
        if row is not None and flag in set(_json_field(row.quality_flags_json) or []):
            return True
    if format_cooldown_seconds > 0:
        return (
            _ai_model_recent_format_failure(
                session,
                provider_id=provider_id,
                model=model,
                analysis_mode=analysis_mode,
                cooldown_seconds=format_cooldown_seconds,
            )
            is not None
        )
    return False


def _openrouter_model_candidates(primary_model: str) -> list[str]:
    return _unique_models([primary_model, *settings.openrouter_fallback_models])


def _ai_provider_candidates(provider) -> list:
    if provider.provider_id != "openrouter":
        return [provider]
    return [
        type(provider)(
            api_key=provider.api_key,
            model=model,
            timeout_seconds=provider.timeout_seconds,
        )
        for model in _openrouter_model_candidates(provider.model)
    ]


def _ai_failure_http_status_code(errors: dict[str, str]) -> int:
    joined = " ".join(errors.values()).lower()
    if "status 429" in joined or "rate limit" in joined or "too many requests" in joined:
        return 429
    if (
        "status 500" in joined
        or "status 502" in joined
        or "status 503" in joined
        or "status 504" in joined
        or "bad gateway" in joined
        or "high demand" in joined
        or "temporarily unavailable" in joined
    ):
        return 503
    return 502


def _ai_analysis_batch_response(
    symbol: str,
    results: dict[str, tuple[StockAIAnalysis, bool]],
    errors: dict[str, str] | None = None,
    running: dict[str, bool] | None = None,
    rule_based: dict[str, StockAIRuleBasedResultResponse] | None = None,
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
        rule_based=StockAIRuleBasedModesResponse(
            unheld=rule_based.get(AI_MODE_UNHELD) if rule_based else None,
            held=rule_based.get(AI_MODE_HELD) if rule_based else None,
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
    input_hash = _ai_cache_input_hash(stock_summary)
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
        evidence_keys=_ai_row_evidence_keys(row),
    )
    if not analysis.format_valid:
        return False
    row.response_json = json.dumps(analysis.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    row.validation_errors_json = json.dumps(validation_errors, ensure_ascii=False)
    row.quality_flags_json = json.dumps(quality_flags_from_validation_errors(validation_errors), ensure_ascii=False)
    row.grounding_errors_json = json.dumps(grounding_errors_from_validation_errors(validation_errors), ensure_ascii=False)
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
    stock_summary = _compact_ai_stock_summary(_ai_stock_summary(stock, session, analysis_mode), analysis_mode)
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
        error_message = str(exc)
        quality_flags = _ai_provider_error_quality_flags(error_message)
        row.request_payload_json = request_payload_json
        row.response_json = "{}"
        row.raw_response_text = None
        row.provider_metadata_json = None
        row.validation_errors_json = json.dumps([], ensure_ascii=False)
        row.quality_flags_json = json.dumps(quality_flags, ensure_ascii=False)
        row.grounding_errors_json = json.dumps([], ensure_ascii=False)
        row.status = "failed"
        row.error_message = error_message
        row.updated_at = now
        session.commit()
        fallback_row = _latest_ai_cache_row(session, stock, provider.provider_id, provider.model, analysis_mode)
        if fallback_row is not None:
            return fallback_row, True, f"{error_message}；已顯示最近成功快取。", False
        return None, False, error_message, False

    now = datetime.now(UTC)
    row.request_payload_json = request_payload_json
    row.response_json = json.dumps(provider_result.analysis.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    row.raw_response_text = provider_result.raw_response_text
    row.provider_metadata_json = json.dumps(provider_result.provider_metadata, ensure_ascii=False, sort_keys=True)
    row.validation_errors_json = json.dumps(provider_result.validation_errors, ensure_ascii=False)
    row.quality_flags_json = json.dumps(provider_result.quality_flags, ensure_ascii=False)
    row.grounding_errors_json = json.dumps(provider_result.grounding_errors, ensure_ascii=False)
    row.status = "success" if provider_result.analysis.format_valid else "format_fallback"
    row.error_message = None if provider_result.analysis.format_valid else "AI response failed validation."
    row.updated_at = now
    session.commit()
    session.refresh(row)
    if not provider_result.analysis.format_valid:
        return None, False, "AI 回覆未通過格式或內容驗證，已保留 Log 供後續檢查。", False
    return row, False, None, False


def _build_ai_provider_for_row(row: StockAIAnalysis):
    if row.provider == "gemini":
        if not settings.gemini_api_key:
            raise AIConfigurationError("GEMINI_API_KEY is not configured.")
        return GeminiProvider(api_key=settings.gemini_api_key, model=row.model)
    if row.provider == "openrouter":
        if not settings.openrouter_api_key:
            raise AIConfigurationError("OPENROUTER_API_KEY is not configured.")
        return OpenRouterProvider(api_key=settings.openrouter_api_key, model=row.model)
    raise AIConfigurationError(f"Unsupported AI provider: {row.provider}")


def _store_ai_provider_success(session: Session, row: StockAIAnalysis, provider_result) -> None:
    now = datetime.now(UTC)
    row.response_json = json.dumps(provider_result.analysis.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    row.raw_response_text = provider_result.raw_response_text
    row.provider_metadata_json = json.dumps(provider_result.provider_metadata, ensure_ascii=False, sort_keys=True)
    row.validation_errors_json = json.dumps(provider_result.validation_errors, ensure_ascii=False)
    row.quality_flags_json = json.dumps(provider_result.quality_flags, ensure_ascii=False)
    row.grounding_errors_json = json.dumps(provider_result.grounding_errors, ensure_ascii=False)
    row.status = "success" if provider_result.analysis.format_valid else "format_fallback"
    row.error_message = None if provider_result.analysis.format_valid else "AI response failed validation."
    row.updated_at = now
    session.commit()


def _store_ai_provider_failure(session: Session, row: StockAIAnalysis, error_message: str) -> None:
    now = datetime.now(UTC)
    quality_flags = _ai_provider_error_quality_flags(error_message)
    row.response_json = "{}"
    row.raw_response_text = None
    row.provider_metadata_json = None
    row.validation_errors_json = json.dumps([], ensure_ascii=False)
    row.quality_flags_json = json.dumps(quality_flags, ensure_ascii=False)
    row.grounding_errors_json = json.dumps([], ensure_ascii=False)
    row.status = "failed"
    row.error_message = error_message
    row.updated_at = now
    session.commit()


def _run_ai_analysis_job(row_id: int) -> None:
    with AI_ANALYSIS_JOB_LOCK:
        _run_ai_analysis_job_locked(row_id)


def _run_ai_analysis_job_locked(row_id: int) -> None:
    with SessionLocal() as session:
        row = session.get(StockAIAnalysis, row_id)
        if row is None or not _ai_analysis_is_fresh_inflight(row):
            return
        try:
            provider = _build_ai_provider_for_row(row)
            stock_summary = json.loads(row.request_payload_json)
        except (AIConfigurationError, json.JSONDecodeError) as exc:
            _store_ai_provider_failure(session, row, str(exc))
            return

        row.status = "running"
        row.updated_at = datetime.now(UTC)
        session.commit()
        try:
            provider_result = provider.analyze_stock(stock_summary, row.analysis_mode)
        except AIAnalysisError as exc:
            _store_ai_provider_failure(session, row, str(exc))
            return
        _store_ai_provider_success(session, row, provider_result)


def _enqueue_ai_mode(
    session: Session,
    stock: Stock,
    provider,
    analysis_mode: str,
    force_refresh: bool,
) -> tuple[StockAIAnalysis | None, bool, str | None, bool, int | None]:
    stock_summary = _compact_ai_stock_summary(_ai_stock_summary(stock, session, analysis_mode), analysis_mode)
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
        return None, False, "AI 分析正在處理中，完成後會自動讀取快取。", True, None
    if row and _ai_analysis_is_cacheable(row) and not force_refresh:
        return row, True, None, False, None
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
            return repaired_row, True, None, False, None

    queued_row = StockAIAnalysis(
        stock_id=stock.id,
        provider=provider.provider_id,
        model=provider.model,
        analysis_mode=analysis_mode,
        prompt_version=PROMPT_VERSION,
        analysis_date=analysis_date,
        input_hash=input_hash,
        request_payload_json=json.dumps(stock_summary, ensure_ascii=False, sort_keys=True),
        response_json="{}",
        validation_errors_json="[]",
        quality_flags_json="[]",
        grounding_errors_json="[]",
        status="queued",
    )
    session.add(queued_row)
    session.commit()
    session.refresh(queued_row)
    return None, False, None, True, queued_row.id


def _enqueue_ai_mode_with_fallback(
    session: Session,
    stock: Stock,
    provider,
    analysis_mode: str,
    force_refresh: bool,
) -> tuple[StockAIAnalysis | None, bool, str | None, bool, int | None]:
    skipped_models: list[str] = []
    for candidate in _ai_provider_candidates(provider):
        if _ai_model_in_cooldown(
            session,
            provider_id=candidate.provider_id,
            model=candidate.model,
            analysis_mode=analysis_mode,
        ):
            skipped_models.append(candidate.model)
            continue
        row, cached, error, running, queued_row_id = _enqueue_ai_mode(
            session,
            stock,
            candidate,
            analysis_mode,
            force_refresh,
        )
        if row is not None or running:
            return row, cached, error, running, queued_row_id
        if error:
            return None, False, error, False, None
    if skipped_models:
        return (
            None,
            False,
            f"所有 OpenRouter 模型仍在冷卻中，請稍後再試：{', '.join(skipped_models)}",
            False,
            None,
        )
    return None, False, "AI analysis failed.", False, None


def _generate_ai_mode_with_fallback(
    session: Session,
    stock: Stock,
    provider,
    analysis_mode: str,
    force_refresh: bool,
) -> tuple[StockAIAnalysis | None, bool, str | None, bool]:
    candidates = _ai_provider_candidates(provider)
    skipped_models: list[str] = []
    errors: list[str] = []
    any_running = False

    for candidate in candidates:
        if _ai_model_in_cooldown(
            session,
            provider_id=candidate.provider_id,
            model=candidate.model,
            analysis_mode=analysis_mode,
        ):
            skipped_models.append(candidate.model)
            continue
        row, cached, error, is_running = _generate_ai_mode(
            session,
            stock,
            candidate,
            analysis_mode,
            force_refresh,
        )
        if row is not None:
            if errors or skipped_models:
                messages = []
                if skipped_models:
                    messages.append(f"已略過冷卻中的模型：{', '.join(skipped_models)}")
                if errors:
                    messages.append("；".join(errors))
                if messages and error:
                    error = f"{'；'.join(messages)}；{error}"
            return row, cached, error, False
        if is_running:
            any_running = True
            errors.append(f"{candidate.model}: AI 分析正在處理中")
            continue
        if error:
            errors.append(f"{candidate.model}: {error}")

    if any_running:
        return None, False, "AI 分析正在處理中，完成後會自動讀取快取。", True
    if skipped_models and not errors:
        return (
            None,
            False,
            f"所有 OpenRouter 模型仍在冷卻中，請稍後再試：{', '.join(skipped_models)}",
            False,
        )
    message = "；".join(errors)
    if skipped_models:
        message = f"{message}；已略過冷卻中的模型：{', '.join(skipped_models)}" if message else f"已略過冷卻中的模型：{', '.join(skipped_models)}"
    return None, False, message or "AI analysis failed.", False


def _json_field(value: str | None):
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _ai_feedback_record(feedback: StockAIFeedback | None) -> dict | None:
    if feedback is None:
        return None
    return {
        "rating": feedback.rating,
        "tags": _json_field(feedback.tags_json) or [],
        "note": feedback.note,
        "created_at": feedback.created_at.isoformat(),
        "updated_at": feedback.updated_at.isoformat(),
    }


def _ai_log_record(row: StockAIAnalysis, symbol: str, feedback: StockAIFeedback | None = None) -> dict:
    run = getattr(row, "run", None)
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
        "quality_flags": _json_field(row.quality_flags_json) or [],
        "grounding_errors": _json_field(row.grounding_errors_json) or [],
        "run": None if run is None else {
            "id": run.id,
            "status": run.status,
            "requested_modes": _json_field(run.requested_modes_json) or [],
            "prompt_version": run.prompt_version,
            "rule_version": run.rule_version,
            "snapshot_hash": run.snapshot_hash,
            "request_strategy": run.request_strategy,
            "data_as_of": _json_field(run.data_as_of_json) or [],
            "stale_items": _json_field(run.stale_items_json) or [],
            "analysis_snapshot": _json_field(run.analysis_snapshot_json),
            "requested_at": run.requested_at.isoformat() if run.requested_at else None,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        },
        "feedback": _ai_feedback_record(feedback),
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




__all__ = [name for name in globals() if not name.startswith("__")]
