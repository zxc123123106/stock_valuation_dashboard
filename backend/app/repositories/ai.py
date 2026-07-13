"""AI analysis persistence queries."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import StockAIAnalysis, StockAIFeedback


def latest_success(session: Session, stock_id: int, mode: str) -> StockAIAnalysis | None:
    return session.scalar(
        select(StockAIAnalysis)
        .where(
            StockAIAnalysis.stock_id == stock_id,
            StockAIAnalysis.analysis_mode == mode,
            StockAIAnalysis.status == "success",
        )
        .order_by(StockAIAnalysis.updated_at.desc())
        .limit(1)
    )


def feedback_for_analysis(session: Session, analysis_id: int) -> StockAIFeedback | None:
    return session.scalar(select(StockAIFeedback).where(StockAIFeedback.analysis_id == analysis_id))
