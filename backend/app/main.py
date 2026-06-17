from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, time
from decimal import Decimal, InvalidOperation

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from .brokers import BrokerConfig, broker_options, get_broker, transaction_tax_rate
from .config import get_settings
from .database import (
    CrawlerLog,
    DATABASE_URL,
    Stock,
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
    FundamentalResponse,
    HealthResponse,
    MetadataResponse,
    BrokerTradingResponse,
    BrokerTradingRowResponse,
    RefreshQueueResponse,
    RefreshStatusResponse,
    StockDeleteResponse,
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
        data_source="TWSE/FinMind quote + TWSE OpenAPI PE + FinMind EPS/fundamentals/daily prices + Yahoo broker trading",
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
    )


@app.get("/api/settings/broker", response_model=BrokerSettingResponse)
def get_broker_setting(session: Session = Depends(get_session)) -> BrokerSettingResponse:
    return _broker_setting_response(session)


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
