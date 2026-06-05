from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import (
    CrawlerLog,
    DATABASE_URL,
    Stock,
    StockBrokerTrading,
    StockBrokerTradingRow,
    StockEPS,
    StockMetric,
    StockPosition,
    StockRefreshState,
    StockValuation,
    get_session,
    init_database,
    ping_database,
)
from .schemas import (
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
)
from .refresh_worker import BackgroundRefreshManager
from .valuation import quantize_money, valuation_status
from .wantgoo import normalize_symbol


settings = get_settings()
refresh_manager = BackgroundRefreshManager(
    interval_seconds=settings.background_refresh_seconds,
    wantgoo_base_url=settings.wantgoo_base_url,
)


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


def _position_response(position: StockPosition | None, metric: StockMetric | None) -> StockPositionResponse | None:
    if not position:
        return None

    profit_loss = None
    profit_loss_percent = None
    if metric:
        profit_loss = metric.current_price - position.buy_price
        profit_loss_percent = _percent(profit_loss, position.buy_price)

    return StockPositionResponse(
        buy_price=_float(position.buy_price),
        unrealized_profit_loss=_float(profit_loss) if profit_loss is not None else None,
        unrealized_profit_loss_percent=_float(profit_loss_percent) if profit_loss_percent is not None else None,
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


def _stock_response(stock: Stock, session: Session) -> StockResponse:
    metric = _latest_metric(session, stock.id)
    position = session.scalar(select(StockPosition).where(StockPosition.stock_id == stock.id))
    valuations = []
    if stock.asset_type != "ETF":
        valuations = session.scalars(
            select(StockValuation)
            .where(StockValuation.stock_id == stock.id)
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
            current_price=_float(metric.current_price),
            current_pe=_float(metric.current_pe),
            price_updated_at=metric.price_updated_at,
            pe_updated_at=metric.pe_updated_at,
            source=metric.source,
        )
        if metric
        else None,
        position=_position_response(position, metric),
        broker_trading=_broker_trading_response(stock, session),
        valuations=[_valuation_response(valuation, position.buy_price if position else None) for valuation in valuations],
    )


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
        data_source="WantGoo quote + TWSE OpenAPI PE + HiStock EPS + Yahoo broker trading",
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
