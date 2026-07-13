from datetime import date, datetime

from pydantic import BaseModel


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


class FundamentalTrendSummaryResponse(BaseModel):
    key: str
    label: str
    value: float | None = None
    value_type: str = "number"


class FundamentalTrendPointResponse(BaseModel):
    period: str
    date: date
    value: float | None = None
    yoy_percent: float | None = None
    mom_percent: float | None = None
    sos_percent: float | None = None
    ttm_eps_yoy_percent: float | None = None


class FundamentalTrendCategoryResponse(BaseModel):
    key: str
    label: str
    unit: str
    summary: list[FundamentalTrendSummaryResponse]
    points: list[FundamentalTrendPointResponse]
    source: str | None = None
    fetched_at: datetime | None = None


class FundamentalTrendsResponse(BaseModel):
    symbol: str
    categories: list[FundamentalTrendCategoryResponse]
