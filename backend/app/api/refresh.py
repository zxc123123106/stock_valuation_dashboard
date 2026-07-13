from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status

from ..refresh.manager import BackgroundRefreshManager
from ..schema.refresh import RefreshQueueResponse, RefreshStatusResponse
from .dependencies import get_refresh_manager


router = APIRouter()


@router.get("/api/refresh/status", response_model=RefreshStatusResponse)
async def refresh_status(
    manager: BackgroundRefreshManager = Depends(get_refresh_manager),
) -> RefreshStatusResponse:
    return RefreshStatusResponse(**await manager.snapshot())


@router.post("/api/stocks/refresh", response_model=RefreshQueueResponse, status_code=status.HTTP_202_ACCEPTED)
async def refresh_all_stocks(
    manager: BackgroundRefreshManager = Depends(get_refresh_manager),
) -> RefreshQueueResponse:
    states = await manager.queue_active_stocks(force_full=True)
    queued_at = datetime.now(UTC)
    symbols = [state.symbol for state in states]
    if states:
        queued_at = min((state.queued_at for state in states if state.queued_at), default=queued_at)
    return RefreshQueueResponse(
        status="queued" if symbols else "idle",
        symbols=symbols,
        queued_at=queued_at,
        message="Active stocks queued for full data refresh." if symbols else "No active stocks to refresh.",
    )


@router.post("/api/stocks/{symbol}/refresh", response_model=RefreshQueueResponse, status_code=status.HTTP_202_ACCEPTED)
async def refresh_stock(
    symbol: str,
    manager: BackgroundRefreshManager = Depends(get_refresh_manager),
) -> RefreshQueueResponse:
    try:
        state = await manager.queue_symbol(symbol, create_placeholder=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RefreshQueueResponse(
        status=state.status,
        symbol=state.symbol,
        queued_at=state.queued_at or datetime.now(UTC),
        message=state.message,
    )
