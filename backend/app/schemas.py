from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    app_env: str
    database: str
    api_version: str


class MetadataResponse(BaseModel):
    data_source: str
    api_version: str
    stocks_count: int
    valuations_count: int
    refresh_status: str | None = None
    refresh_interval_seconds: int | None = None
    next_auto_refresh_at: datetime | None = None
    last_refresh_finished_at: datetime | None = None


class StockMetricResponse(BaseModel):
    current_price: float
    current_pe: float
    price_updated_at: datetime
    pe_updated_at: datetime
    source: str


class StockValuationResponse(BaseModel):
    eps_type: str
    eps_value: float
    eps_period: str | None = None
    estimated_price: float
    price_difference: float
    difference_percent: float
    cost_difference: float | None = None
    cost_difference_percent: float | None = None
    valuation_status: str
    source: str
    calculated_at: datetime


class StockPositionResponse(BaseModel):
    buy_price: float
    unrealized_profit_loss: float | None = None
    unrealized_profit_loss_percent: float | None = None


class BrokerTradingRowResponse(BaseModel):
    rank: int
    broker_name: str
    buy_volume: int
    sell_volume: int
    net_volume: int


class BrokerTradingResponse(BaseModel):
    trade_date: str
    main_net_volume: int
    main_buy_volume: int
    main_sell_volume: int
    volume_ratio_percent: float | None = None
    buy_brokers: list[BrokerTradingRowResponse]
    sell_brokers: list[BrokerTradingRowResponse]
    source: str
    fetched_at: datetime


class StockResponse(BaseModel):
    symbol: str
    name: str
    asset_type: str
    market: str
    currency: str
    is_active: bool
    display_order: int
    metric: StockMetricResponse | None
    position: StockPositionResponse | None = None
    broker_trading: BrokerTradingResponse | None = None
    valuations: list[StockValuationResponse]


class StockPositionRequest(BaseModel):
    buy_price: float


class RefreshQueueResponse(BaseModel):
    status: str
    symbol: str | None = None
    symbols: list[str] = []
    queued_at: datetime
    message: str


class RefreshSymbolStateResponse(BaseModel):
    symbol: str
    status: str
    message: str
    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RefreshStatusResponse(BaseModel):
    status: str
    current_symbol: str | None = None
    queue_length: int
    next_auto_refresh_at: datetime | None = None
    last_refresh_finished_at: datetime | None = None
    symbols: list[RefreshSymbolStateResponse]


class StockReorderRequest(BaseModel):
    symbols: list[str]


class StockDeleteResponse(BaseModel):
    status: str
    symbol: str
