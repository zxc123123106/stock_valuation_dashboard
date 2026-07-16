from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class StockAIAnalysisRequest(BaseModel):
    provider: str | None = None
    force_refresh: bool = False


class StockAIAnalysisEvidenceText(BaseModel):
    text: str
    evidence_keys: list[str] = Field(default_factory=list)


class StockAIAnalysisContent(BaseModel):
    overall_status: str
    summary: str | StockAIAnalysisEvidenceText
    positive_points: list[str | StockAIAnalysisEvidenceText]
    risk_points: list[str | StockAIAnalysisEvidenceText]
    watch_points: list[str | StockAIAnalysisEvidenceText]
    disclaimer: str
    format_valid: bool = True


class StockAIAnalysisResultResponse(BaseModel):
    id: int
    mode: str
    provider: str
    model: str
    prompt_version: str
    cached: bool
    analysis_date: date
    analysis_requested_at: datetime | None = None
    generated_at: datetime
    analysis: StockAIAnalysisContent


class StockAIAnalysisModesResponse(BaseModel):
    unheld: StockAIAnalysisResultResponse | None = None
    held: StockAIAnalysisResultResponse | None = None


class StockAIRuleBasedResultResponse(BaseModel):
    mode: str
    source: str = "rule_based"
    generated_at: datetime
    analysis: StockAIAnalysisContent


class StockAIRuleBasedModesResponse(BaseModel):
    unheld: StockAIRuleBasedResultResponse | None = None
    held: StockAIRuleBasedResultResponse | None = None


class StockAIDataAsOfResponse(BaseModel):
    category: str
    label: str
    data_date: date | None = None
    data_period: str | None = None
    fetched_at: datetime | None = None
    source: str | None = None
    freshness_status: str
    is_cached: bool = False


class AIProviderHealthResponse(BaseModel):
    provider: str
    model: str
    status: str
    configured: bool = True
    consecutive_failures: int = 0
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_http_status: int | None = None
    last_error_summary: str | None = None
    cooldown_until: datetime | None = None


class StockAIAnalysisRunResponse(BaseModel):
    id: int
    status: str
    requested_modes: list[str]
    provider: str | None = None
    model: str | None = None
    prompt_version: str
    rule_version: str
    request_strategy: str
    snapshot_hash: str
    requested_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class StockAIAnalysisResponse(BaseModel):
    symbol: str
    analyses: StockAIAnalysisModesResponse
    rule_based: StockAIRuleBasedModesResponse = Field(default_factory=StockAIRuleBasedModesResponse)
    errors: dict[str, str] = Field(default_factory=dict)
    running: dict[str, bool] = Field(default_factory=dict)
    run: StockAIAnalysisRunResponse | None = None
    provider_health: list[AIProviderHealthResponse] = Field(default_factory=list)
    data_as_of: list[StockAIDataAsOfResponse] = Field(default_factory=list)
    stale_items: list[str] = Field(default_factory=list)
    request_strategy: str | None = None


class StockAIAnalysisFeedbackRequest(BaseModel):
    analysis_id: int | None = None
    rating: Literal["useful", "not_useful"]
    tags: list[
        Literal[
            "hallucination",
            "too_generic",
            "wrong_status",
            "wrong_number",
            "missing_context",
            "format_issue",
        ]
    ] = Field(default_factory=list)
    note: str | None = Field(default=None, max_length=500)


class StockAIAnalysisFeedbackResponse(BaseModel):
    status: str
    analysis_id: int
    rating: str
    tags: list[str]
    note: str | None = None
    updated_at: datetime
