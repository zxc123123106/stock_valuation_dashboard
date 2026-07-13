"""Per-category data-quality state queries."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import StockDataQualityState


def list_for_stock(session: Session, stock_id: int) -> list[StockDataQualityState]:
    return list(
        session.scalars(
            select(StockDataQualityState)
            .where(StockDataQualityState.stock_id == stock_id)
            .order_by(StockDataQualityState.category)
        ).all()
    )
