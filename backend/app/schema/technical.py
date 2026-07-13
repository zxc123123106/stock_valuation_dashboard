from datetime import date, datetime

from pydantic import BaseModel


class TechnicalCandleResponse(BaseModel):
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    volume_ma5: float | None = None
    volume_ma20: float | None = None
    volume_vs_ma20_percent: float | None = None
    ma5: float | None = None
    ma10: float | None = None
    ma20: float | None = None
    ma60: float | None = None
    ma120: float | None = None
    ma240: float | None = None
    is_provisional: bool = False


class TechnicalAnalysisResponse(BaseModel):
    symbol: str
    interval: str
    source: str
    fetched_at: datetime | None = None
    candles: list[TechnicalCandleResponse]
