from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..db.session import get_session
from ..refresh.manager import BackgroundRefreshManager
from ..schema.stock import (
    FundamentalTrendsResponse,
    StockDataQualityResponse,
    StockDeleteResponse,
    StockPositionRequest,
    StockReorderRequest,
    StockResponse,
    StockValuationResponse,
    TechnicalAnalysisResponse,
)
from ..services import stock_service
from ..services.dashboard_service import DashboardSnapshotCache
from .dependencies import get_dashboard_snapshot_cache, get_refresh_manager


router = APIRouter()


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, stock_service.StockNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/api/stocks", response_model=list[StockResponse])
def list_stocks(session: Session = Depends(get_session)) -> list[StockResponse]:
    return stock_service.list_stocks(session)


@router.post("/api/stocks/reorder", response_model=list[StockResponse])
def reorder_stocks(
    payload: StockReorderRequest,
    session: Session = Depends(get_session),
    snapshot_cache: DashboardSnapshotCache = Depends(get_dashboard_snapshot_cache),
) -> list[StockResponse]:
    try:
        response = stock_service.reorder_stocks(session, payload.symbols)
        snapshot_cache.invalidate()
        return response
    except (stock_service.StockValidationError, stock_service.StockNotFoundError) as exc:
        raise _http_error(exc) from exc


@router.get("/api/stocks/{symbol}", response_model=StockResponse)
def get_stock(symbol: str, session: Session = Depends(get_session)) -> StockResponse:
    try:
        return stock_service.get_stock(session, symbol)
    except stock_service.StockNotFoundError as exc:
        raise _http_error(exc) from exc


@router.get("/api/stocks/{symbol}/fundamentals/trends", response_model=FundamentalTrendsResponse)
def get_stock_fundamental_trends(
    symbol: str,
    session: Session = Depends(get_session),
) -> FundamentalTrendsResponse:
    try:
        return stock_service.get_fundamental_trends(session, symbol)
    except (stock_service.StockValidationError, stock_service.StockNotFoundError) as exc:
        raise _http_error(exc) from exc


@router.get("/api/stocks/{symbol}/technical-analysis", response_model=TechnicalAnalysisResponse)
def get_technical_analysis(
    symbol: str,
    limit: int = Query(default=120, ge=20, le=250),
    session: Session = Depends(get_session),
) -> TechnicalAnalysisResponse:
    try:
        return stock_service.get_technical_analysis(session, symbol, limit)
    except (stock_service.StockValidationError, stock_service.StockNotFoundError) as exc:
        raise _http_error(exc) from exc


@router.get("/api/stocks/{symbol}/data-quality", response_model=StockDataQualityResponse)
def stock_data_quality(symbol: str, session: Session = Depends(get_session)) -> StockDataQualityResponse:
    try:
        return stock_service.get_data_quality(session, symbol)
    except (stock_service.StockValidationError, stock_service.StockNotFoundError) as exc:
        raise _http_error(exc) from exc


@router.delete("/api/stocks/{symbol}", response_model=StockDeleteResponse)
async def delete_stock(
    symbol: str,
    session: Session = Depends(get_session),
    manager: BackgroundRefreshManager = Depends(get_refresh_manager),
    snapshot_cache: DashboardSnapshotCache = Depends(get_dashboard_snapshot_cache),
) -> StockDeleteResponse:
    try:
        normalized = stock_service.normalize_stock_symbol(symbol)
        await manager.forget_symbol(normalized)
        stock_service.delete_stock(session, normalized)
        snapshot_cache.invalidate()
    except (stock_service.StockValidationError, stock_service.StockNotFoundError) as exc:
        raise _http_error(exc) from exc
    return StockDeleteResponse(status="ok", symbol=normalized)


@router.put("/api/stocks/{symbol}/position", response_model=StockResponse)
def set_stock_position(
    symbol: str,
    payload: StockPositionRequest,
    session: Session = Depends(get_session),
    snapshot_cache: DashboardSnapshotCache = Depends(get_dashboard_snapshot_cache),
) -> StockResponse:
    try:
        response = stock_service.set_position(session, symbol, payload.buy_price)
        snapshot_cache.invalidate()
        return response
    except (stock_service.StockValidationError, stock_service.StockNotFoundError) as exc:
        raise _http_error(exc) from exc


@router.delete("/api/stocks/{symbol}/position", response_model=StockResponse)
def clear_stock_position(
    symbol: str,
    session: Session = Depends(get_session),
    snapshot_cache: DashboardSnapshotCache = Depends(get_dashboard_snapshot_cache),
) -> StockResponse:
    try:
        response = stock_service.clear_position(session, symbol)
        snapshot_cache.invalidate()
        return response
    except (stock_service.StockValidationError, stock_service.StockNotFoundError) as exc:
        raise _http_error(exc) from exc


@router.get("/api/stocks/{symbol}/valuations", response_model=list[StockValuationResponse])
def get_stock_valuations(
    symbol: str,
    session: Session = Depends(get_session),
) -> list[StockValuationResponse]:
    try:
        return stock_service.stock_valuations(session, symbol)
    except stock_service.StockNotFoundError as exc:
        raise _http_error(exc) from exc
