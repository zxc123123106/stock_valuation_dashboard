"""WTX cache read queries."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import FuturesIntradayPoint, FuturesSnapshot


def latest_snapshot(session: Session, symbol: str) -> FuturesSnapshot | None:
    return session.scalar(
        select(FuturesSnapshot)
        .where(FuturesSnapshot.symbol == symbol)
        .order_by(FuturesSnapshot.price_updated_at.desc())
        .limit(1)
    )


def session_points(session: Session, symbol: str, session_type: str, session_date):
    return list(
        session.scalars(
            select(FuturesIntradayPoint)
            .where(
                FuturesIntradayPoint.symbol == symbol,
                FuturesIntradayPoint.session_type == session_type,
                FuturesIntradayPoint.session_date == session_date,
            )
            .order_by(FuturesIntradayPoint.point_time)
        ).all()
    )
