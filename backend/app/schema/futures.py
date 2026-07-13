from datetime import datetime

from pydantic import BaseModel, Field


class FuturesChartPointResponse(BaseModel):
    timestamp: datetime
    price: float
    difference_percent: float
    source: str | None = None


class FuturesWtxResponse(BaseModel):
    symbol: str
    name: str
    session_type: str
    session_label: str
    session_start_at: datetime | None = None
    session_end_at: datetime | None = None
    current_price: float | None = None
    open_price: float | None = None
    difference_points: float | None = None
    difference_percent: float | None = None
    price_updated_at: datetime | None = None
    is_stale: bool = True
    chart_points: list[FuturesChartPointResponse] = Field(default_factory=list)
