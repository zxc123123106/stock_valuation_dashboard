from datetime import date, datetime

from pydantic import BaseModel, Field

from .fundamental import *
from .quality import *
from .technical import *


class StockMetricResponse(BaseModel):
    open_price: float | None = None
    previous_close: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    current_price: float
    change_percent: float | None = None
    current_pe: float | None = None
    pe_average_3y: float | None = None
    pe_min_3y: float | None = None
    pe_max_3y: float | None = None
    pe_vs_average_percent: float | None = None
    price_updated_at: datetime
    pe_updated_at: datetime
    pe_data_date: date | None = None
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
    fundamental: FundamentalResponse | None = None
    valuations: list[StockValuationResponse]
    data_quality_summary: DataQualitySummaryResponse | None = None


class StockPositionRequest(BaseModel):
    buy_price: float


class StockReorderRequest(BaseModel):
    symbols: list[str]


class StockDeleteResponse(BaseModel):
    status: str
    symbol: str


__all__ = [name for name in globals() if not name.startswith("_")]
