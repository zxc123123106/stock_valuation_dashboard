from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import Stock, StockDailyPrice, StockMetric, StockValuation
from ..db.session import ping_database


def health_snapshot() -> dict:
    ping_database()
    return {"database": "sqlite"}


def metadata_counts(session: Session) -> dict:
    return {
        "stocks_count": session.scalar(
            select(func.count()).select_from(Stock).where(Stock.is_active.is_(True))
        ) or 0,
        "valuations_count": session.scalar(
            select(func.count())
            .select_from(StockValuation)
            .join(Stock)
            .where(Stock.is_active.is_(True))
        ) or 0,
        "latest_official_data_date": _latest_official_data_date(session),
    }


def _latest_official_data_date(session: Session) -> date | None:
    daily_date = session.scalar(
        select(func.max(StockDailyPrice.trade_date)).join(Stock).where(Stock.is_active.is_(True))
    )
    pe_date = session.scalar(
        select(func.max(StockMetric.pe_data_date)).join(Stock).where(Stock.is_active.is_(True))
    )
    return max((value for value in (daily_date, pe_date) if value is not None), default=None)
