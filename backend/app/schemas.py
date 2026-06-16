from __future__ import annotations

from datetime import date, datetime

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
    auto_refresh_enabled: bool | None = None
    market_session: str | None = None
    refresh_window: str | None = None
    next_auto_refresh_at: datetime | None = None
    last_refresh_finished_at: datetime | None = None
    last_close_verification_at: datetime | None = None


class StockMetricResponse(BaseModel):
    open_price: float | None = None
    previous_close: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    current_price: float
    change_percent: float | None = None
    current_pe: float
    pe_average_3y: float | None = None
    pe_min_3y: float | None = None
    pe_max_3y: float | None = None
    pe_vs_average_percent: float | None = None
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
    fee_adjusted_profit_loss: float | None = None
    fee_adjusted_profit_loss_percent: float | None = None
    broker_id: str
    broker_fee_rate: float


class BrokerOptionResponse(BaseModel):
    broker_id: str
    name: str
    buy_fee_rate: float
    sell_fee_rate: float
    source_url: str


class BrokerSettingResponse(BaseModel):
    selected_broker: str
    selected: BrokerOptionResponse
    brokers: list[BrokerOptionResponse]


class BrokerSettingRequest(BaseModel):
    broker_id: str


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


class TechnicalCandleResponse(BaseModel):
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: int | None = None
    ma20: float | None = None
    is_provisional: bool = False


class TechnicalAnalysisResponse(BaseModel):
    symbol: str
    interval: str
    source: str
    fetched_at: datetime | None = None
    candles: list[TechnicalCandleResponse]


class FundamentalResponse(BaseModel):
    latest_quarter_eps: float | None = None
    eps_yoy_percent: float | None = None
    ttm_eps_yoy_percent: float | None = None
    latest_revenue_yoy_percent: float | None = None
    latest_revenue_mom_percent: float | None = None
    three_month_revenue_yoy_percent: float | None = None
    gross_margin: float | None = None
    gross_margin_sos: float | None = None
    operating_margin: float | None = None
    operating_margin_sos: float | None = None
    net_margin: float | None = None
    net_margin_sos: float | None = None
    source: str | None = None
    fetched_at: datetime | None = None


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
    fundamental: FundamentalResponse | None = None
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
    failure_count: int = 0
    last_error: str | None = None
    next_retry_at: datetime | None = None
    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RefreshStatusResponse(BaseModel):
    status: str
    current_symbol: str | None = None
    queue_length: int
    auto_refresh_enabled: bool = False
    market_session: str = "closed"
    refresh_window: str = ""
    next_auto_refresh_at: datetime | None = None
    last_refresh_finished_at: datetime | None = None
    last_close_verification_at: datetime | None = None
    symbols: list[RefreshSymbolStateResponse]


class StockReorderRequest(BaseModel):
    symbols: list[str]


class StockDeleteResponse(BaseModel):
    status: str
    symbol: str
