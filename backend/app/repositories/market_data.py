"""Read-side queries for cached market data."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import StockMetric


def latest_metric(session: Session, stock_id: int) -> StockMetric | None:
    return session.scalar(
        select(StockMetric)
        .where(StockMetric.stock_id == stock_id)
        .order_by(StockMetric.price_updated_at.desc(), StockMetric.id.desc())
        .limit(1)
    )
