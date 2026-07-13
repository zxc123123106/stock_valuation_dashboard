from datetime import date, datetime

from pydantic import BaseModel, Field


class DataQualityCategorySummaryResponse(BaseModel):
    freshness_status: str
    is_cached: bool = False
    sync_status: str = "idle"


class DataQualitySummaryResponse(BaseModel):
    overall_status: str
    issue_count: int = 0
    categories: dict[str, DataQualityCategorySummaryResponse] = Field(default_factory=dict)


class DataQualityComponentResponse(BaseModel):
    category: str
    label: str
    freshness_status: str
    is_cached: bool = False
    sync_status: str = "idle"
    data_date: date | None = None
    data_period: str | None = None
    fetched_at: datetime | None = None
    source: str | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error_summary: str | None = None
    last_error_detail: str | None = None
    last_error_at: datetime | None = None
    next_retry_at: datetime | None = None


class DataQualityItemResponse(DataQualityComponentResponse):
    components: list[DataQualityComponentResponse] = Field(default_factory=list)


class StockDataQualityResponse(BaseModel):
    symbol: str
    overall_status: str
    issue_count: int
    checked_at: datetime
    items: list[DataQualityItemResponse]
